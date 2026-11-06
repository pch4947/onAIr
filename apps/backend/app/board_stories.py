"""Hand board posts to the engine as story candidates and settle them once read (contract 3.5)."""
from datetime import datetime
import json
import logging
import time
from uuid import uuid4

from .notifications import NotificationStore

log = logging.getLogger(__name__)

POSTS_KEY = 'backend:board:posts'
READ_KEY = 'backend:board:read-stories'
# Set once, the first time this feature runs. Older posts were written before the
# board told authors a post can be read on air, so they never become candidates.
STORIES_SINCE_KEY = 'backend:board:stories-since'
# Segments carry a story in request_ref; the prefix tells it apart from a listener request.
STORY_PREFIX = 'story_'
MAX_CANDIDATES = 30
MAX_AGE_SECONDS = 7 * 24 * 3600
MAX_BODY_LENGTH = 500

# A retried station.started or a new post must never hand the engine the same story twice.
OFFER = """
local kinds = {'string', 'set', 'stream', 'hash'}
for i=1,4 do
 local t = redis.call('TYPE', KEYS[i]).ok
 if t ~= 'none' and t ~= kinds[i] then return redis.error_reply('Unexpected story key type') end
end
local station = redis.call('GET', KEYS[1])
if not station or cjson.decode(station).status ~= 'started' then return 0 end
if redis.call('HEXISTS', KEYS[4], ARGV[1]) == 1 then return 0 end
if redis.call('SADD', KEYS[2], ARGV[1]) == 0 then return 0 end
redis.call('XADD', KEYS[3], '*', 'type', 'board.story', 'contract_version', '1',
 'event_id', ARGV[2], 'station_id', ARGV[3], 'at', ARGV[4], 'payload', ARGV[5])
return 1
"""

WITHDRAW = """
local kinds = {'string', 'set', 'stream'}
for i=1,3 do
 local t = redis.call('TYPE', KEYS[i]).ok
 if t ~= 'none' and t ~= kinds[i] then return redis.error_reply('Unexpected story key type') end
end
local station = redis.call('GET', KEYS[1])
if not station or cjson.decode(station).status ~= 'started' then return 0 end
if redis.call('SREM', KEYS[2], ARGV[1]) == 0 then return 0 end
redis.call('XADD', KEYS[3], '*', 'type', 'board.story.withdrawn', 'contract_version', '1',
 'event_id', ARGV[2], 'station_id', ARGV[3], 'at', ARGV[4], 'payload', ARGV[5])
return 1
"""


def story_id(post_id):
    return STORY_PREFIX + post_id


def posted_at(post):
    return datetime.fromisoformat(post['createdAt']).timestamp()


def eligible(post, now, since):
    written = posted_at(post)
    return len(post['body']) <= MAX_BODY_LENGTH and now - written <= MAX_AGE_SECONDS and written >= since


class StoryFeed:
    def __init__(self, redis, station_id):
        self.redis = redis
        self.station_id = station_id
        self.prefix = f'backend:{station_id}'

    async def stories_since(self):
        await self.redis.setnx(STORIES_SINCE_KEY, str(time.time()))
        return float(await self.redis.get(STORIES_SINCE_KEY))

    @property
    def _keys(self):
        return self.prefix + ':station', self.prefix + ':stories-sent', f'engine:{self.station_id}:in'

    async def _accepts_stories(self):
        raw = await self.redis.get(self.prefix + ':station')
        if not raw:
            return False
        config = json.loads(raw).get('config', {})
        # Only the talk format has a story corner; omitted corners use the engine defaults.
        corners = config.get('corners')
        return config.get('format', 'talk') == 'talk' and (corners is None or 'story_corner' in corners)

    async def _send(self, post):
        payload = dict(story_id=story_id(post['id']), body=post['body'], author_ref=post['authorId'],
                       nickname=post['authorName'], posted_at=posted_at(post), tags=[])
        return await self.redis.eval(OFFER, 4, *self._keys, READ_KEY, payload['story_id'], 'evt_' + uuid4().hex,
                                     self.station_id, str(time.time()), json.dumps(payload, ensure_ascii=False))

    async def offer_recent(self):
        """Send the newest unread posts when a broadcast starts; the engine picks what to read."""
        if not await self._accepts_stories():
            return
        posts = [json.loads(raw) for raw in (await self.redis.hgetall(POSTS_KEY)).values()]
        now, since = time.time(), await self.stories_since()
        recent = sorted((post for post in posts if eligible(post, now, since)), key=posted_at, reverse=True)
        sent = 0
        for post in recent:
            sent += await self._send(post)
            if sent == MAX_CANDIDATES:
                break

    async def offer(self, post):
        """Send a post written during the broadcast."""
        if await self._accepts_stories() and eligible(post, time.time(), await self.stories_since()):
            await self._send(post)

    async def withdraw(self, post_id):
        """Pull a deleted post back; a segment already submitted still plays."""
        await self.redis.eval(WITHDRAW, 3, *self._keys, story_id(post_id), 'evt_' + uuid4().hex, self.station_id,
                              str(time.time()), json.dumps(dict(story_id=story_id(post_id))))


async def settle_read(redis, station_id, read_story_id):
    """Mark a story read on PUBLISHED and tell its author once, even if delivery is retried."""
    if not await redis.hsetnx(READ_KEY, read_story_id, str(time.time())):
        return
    post_id = read_story_id.removeprefix(STORY_PREFIX)
    raw_post = await redis.hget(POSTS_KEY, post_id)
    if not raw_post:
        return
    post = json.loads(raw_post)
    raw_station = await redis.get(f'backend:{station_id}:station')
    persona = (json.loads(raw_station).get('config', {}).get('persona') or {}) if raw_station else {}
    show = f"{persona['dj_name']} 방송" if persona.get('dj_name') else '방송'
    await NotificationStore(redis).create(post['authorId'], 'story_read', '내 사연이 방송에서 읽혔어요',
                                          f"{show}에서 '{post['title']}' 사연을 읽었어요", data={'postId': post_id})
