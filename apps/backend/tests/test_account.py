"""계정 설정(#325): 닉네임·비밀번호 변경, 기본 익명, 탈퇴 — 실제 Redis로 확인한다."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
import time
import unittest
import uuid

import httpx
import jwt
from redis.asyncio import Redis

from app.account import WITHDRAWN_NAME
from app.auth import JWT_ALGORITHM
from app.board import LikeStore
from app.board_stories import POSTS_KEY
from app.config import Settings
from app.main import create_app
from app.notifications import NotificationStore
from app.users import UserStore

PASSWORD = 'password1234'


def iso(seconds_ago=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat(timespec='milliseconds')


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1', 'Requires Redis')
class AccountApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tag = uuid.uuid4().hex[:8]
        self.sid = 'st_acct_' + self.tag
        self.redis = Redis.from_url('redis://127.0.0.1:6379/15', decode_responses=True)
        self.app = create_app(replace(Settings('127.0.0.1', 3000, station_id=self.sid), admin_pins={'9999': '관리자'}))
        self.app.state.redis = self.redis
        self.app.state.users = UserStore(self.redis)
        self.app.state.post_likes = LikeStore(self.redis, 'backend:board:post-likes', 'backend:board:user-liked-posts')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url='http://test')
        self.emails, self.posts = [], []
        self.me = await self.signup('나' + self.tag)
        self.other = await self.signup('남' + self.tag)

    async def asyncTearDown(self):
        await self.client.aclose()
        for email in self.emails:
            raw = await self.redis.hget('backend:users', email)
            if raw:
                await self.redis.hdel('backend:usernames', json.loads(raw)['name'])
            await self.redis.hdel('backend:users', email)
        for name in await self.redis.hkeys('backend:usernames'):
            if self.tag in name:
                await self.redis.hdel('backend:usernames', name)
        if self.posts:
            await self.redis.hdel(POSTS_KEY, *self.posts)
        keys = [k async for k in self.redis.scan_iter(match=f'backend:{self.sid}:*')]
        keys += [f'backend:board:comments:{p}' for p in self.posts]
        keys += [f'backend:board:post-likes:{p}' for p in self.posts]
        for user in (self.me, self.other):
            keys += [f'backend:board:user-liked-posts:{user["id"]}', f'backend:notifications:{user["id"]}',
                     f'backend:notifications:{user["id"]}:index', f'backend:notifications:{user["id"]}:unread']
        await self.redis.delete(*keys)
        await self.redis.aclose()

    async def signup(self, name):
        email = f'acct_{uuid.uuid4().hex[:10]}@example.com'
        self.emails.append(email)
        res = await self.client.post('/api/auth/signup', json={'email': email, 'password': PASSWORD, 'name': name})
        self.assertEqual(res.status_code, 201, res.text)
        body = res.json()
        return {**body['user'], 'token': body['accessToken'], 'email': email}

    def auth(self, user):
        return {'Authorization': f'Bearer {user["token"]}'}

    def token_issued(self, user, seconds_ago):
        """다른 기기에서 예전에 로그인해 둔 토큰."""
        now = datetime.now(timezone.utc)
        return {'token': jwt.encode({'sub': user['email'], 'uid': user['id'], 'iat': now - timedelta(seconds=seconds_ago),
                                     'exp': now + timedelta(days=1)}, self.app.state.settings.jwt_secret, JWT_ALGORITHM)}

    async def me_status(self, user):
        return (await self.client.get('/api/auth/me', headers=self.auth(user))).status_code

    async def seed_content(self):
        """내 글·댓글(다른 사람 글에), 다른 사람 글·댓글, 이 채널의 내 요청(이름·익명)과 다른 사람 요청."""
        mine_post, other_post = 'p_mine_' + self.tag, 'p_other_' + self.tag
        self.posts += [mine_post, other_post]
        for post_id, user in ((mine_post, self.me), (other_post, self.other)):
            await self.redis.hset(POSTS_KEY, post_id, json.dumps({
                'id': post_id, 'authorId': user['id'], 'authorName': user['name'], 'title': 't', 'body': 'b',
                'songRequestUrl': None, 'createdAt': iso()}, ensure_ascii=False))
        await self.redis.rpush(f'backend:board:comments:{other_post}', *[json.dumps({
            'id': cid, 'postId': other_post, 'authorId': user['id'], 'authorName': user['name'], 'body': 'c',
            'createdAt': iso()}, ensure_ascii=False) for cid, user in (('c1', self.other), ('c2', self.me), ('c3', self.other))])
        rows = [('r_named', self.me, self.me['name'], 'story', 30), ('r_anon', self.me, None, 'message', 20),
                ('r_song', self.me, self.me['name'], 'song_request', 10), ('r_other', self.other, self.other['name'], 'story', 5)]
        for rid, user, name, kind, ago in rows:
            await self.redis.hset(f'backend:{self.sid}:requests', rid, json.dumps({
                'id': rid, 'prompt': rid, 'listenerId': user['id'], 'listenerName': name, 'kind': kind,
                'status': 'requested', 'trackTitle': '밤편지' if kind == 'song_request' else None,
                'trackArtist': None, 'trackThumbnailUrl': None, 'createdAt': iso(ago)}, ensure_ascii=False))
        await self.redis.hset(f'backend:{self.sid}:request-states', 'r_named', json.dumps({'state': 'reflected'}))
        return mine_post, other_post

    async def names(self):
        posts = {p: json.loads(await self.redis.hget(POSTS_KEY, p))['authorName'] for p in self.posts}
        comments = [json.loads(c)['authorName'] for c in await self.redis.lrange(f'backend:board:comments:{self.posts[1]}', 0, -1)]
        requests = {k: json.loads(v)['listenerName'] for k, v in (await self.redis.hgetall(f'backend:{self.sid}:requests')).items()}
        return posts, comments, requests

    async def test_rename_updates_my_name_everywhere_but_keeps_anonymous(self):
        await self.seed_content()
        new = '새이름' + self.tag
        res = await self.client.post('/api/account/name', json={'name': f'  {new} '}, headers=self.auth(self.me))
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()['user']['name'], new)
        posts, comments, requests = await self.names()
        self.assertEqual(posts, {self.posts[0]: new, self.posts[1]: self.other['name']})
        self.assertEqual(comments, [self.other['name'], new, self.other['name']])
        self.assertEqual(requests, {'r_named': new, 'r_anon': None, 'r_song': new, 'r_other': self.other['name']})
        self.assertEqual((await self.client.get('/api/auth/me', headers=self.auth(self.me))).json()['user']['name'], new)
        # 이전 이름은 비고, 새 이름은 내 것이다
        self.assertEqual((await self.client.post('/api/auth/signup', json={
            'email': f'acct_{uuid.uuid4().hex[:10]}@example.com', 'password': PASSWORD, 'name': new})).status_code, 409)
        free = await self.client.post('/api/auth/signup', json={
            'email': (email := f'acct_{uuid.uuid4().hex[:10]}@example.com'), 'password': PASSWORD, 'name': self.me['name']})
        self.emails.append(email)
        self.assertEqual(free.status_code, 201)

    async def test_rename_rejects_taken_reserved_and_bad_names(self):
        self.assertEqual((await self.client.post('/api/account/name', json={'name': self.other['name']},
                                                  headers=self.auth(self.me))).status_code, 409)
        self.assertEqual((await self.client.post('/api/account/name', json={'name': '관리자'},
                                                  headers=self.auth(self.me))).status_code, 409)
        for name in ['', '   ', 'x' * 51]:
            with self.subTest(name=name):
                self.assertEqual((await self.client.post('/api/account/name', json={'name': name},
                                                          headers=self.auth(self.me))).status_code, 422)
        # 탈퇴한 계정에 남는 이름은 누구도 닉네임으로 쓰지 못한다 (#344)
        self.assertEqual((await self.client.post('/api/account/name', json={'name': f' {WITHDRAWN_NAME} '},
                                                  headers=self.auth(self.me))).status_code, 409)
        self.assertEqual((await self.client.post('/api/auth/guest', json={'name': WITHDRAWN_NAME})).status_code, 409)
        self.assertEqual((await self.client.post('/api/auth/signup', json={
            'email': f'acct_{uuid.uuid4().hex[:10]}@example.com', 'password': PASSWORD,
            'name': WITHDRAWN_NAME})).status_code, 409)
        # 지금 이름 그대로는 바꿀 것이 없어도 성공한다
        same = await self.client.post('/api/account/name', json={'name': self.me['name']}, headers=self.auth(self.me))
        self.assertEqual(same.status_code, 200)

    async def test_password_change(self):
        wrong = await self.client.post('/api/account/password', json={'currentPassword': 'nope', 'newPassword': 'newpass1234'},
                                       headers=self.auth(self.me))
        self.assertEqual(wrong.status_code, 403)
        short = await self.client.post('/api/account/password', json={'currentPassword': PASSWORD, 'newPassword': 'short'},
                                       headers=self.auth(self.me))
        self.assertEqual(short.status_code, 422)
        elsewhere = self.token_issued(self.me, 60)
        self.assertEqual(await self.me_status(elsewhere), 200)
        ok = await self.client.post('/api/account/password', json={'currentPassword': PASSWORD, 'newPassword': 'newpass1234'},
                                    headers=self.auth(self.me))
        self.assertEqual(ok.status_code, 204)
        login = lambda pw: self.client.post('/api/auth/login', json={'email': self.me['email'], 'password': pw})  # noqa: E731
        self.assertEqual((await login(PASSWORD)).status_code, 401)
        again = await login('newpass1234')
        self.assertEqual(again.status_code, 200)
        # 바꾸기 전에 발급된 다른 기기의 토큰은 버린다. 바꾼 이 기기와 새로 로그인한 토큰은 그대로 쓴다 (#344)
        self.assertEqual(await self.me_status(elsewhere), 401)
        self.assertEqual(await self.me_status(self.me), 200)
        self.assertEqual(await self.me_status({'token': again.json()['accessToken']}), 200)
        # 한 번 더 바꾸면 방금까지 쓰던 다른 토큰도 버려진다
        second = {'token': again.json()['accessToken']}
        stale = self.token_issued(self.me, 30)
        self.assertEqual((await self.client.post('/api/account/password', headers=self.auth(second), json={
            'currentPassword': 'newpass1234', 'newPassword': 'thirdpass1234'})).status_code, 204)
        self.assertEqual(await self.me_status(stale), 401)
        self.assertEqual(await self.me_status(second), 200)

    async def test_token_of_a_deleted_account_does_not_open_a_new_one_with_the_same_email(self):
        self.assertEqual((await self.client.request('DELETE', '/api/account', json={'password': PASSWORD},
                                                    headers=self.auth(self.me))).status_code, 204)
        self.assertEqual(await self.me_status(self.me), 401)
        again = await self.client.post('/api/auth/signup', json={
            'email': self.me['email'], 'password': 'another1234', 'name': '다시' + self.tag})
        self.assertEqual(again.status_code, 201, again.text)
        self.assertNotEqual(again.json()['user']['id'], self.me['id'])
        self.assertEqual(await self.me_status(self.me), 401)  # 탈퇴 전 토큰 (#344)
        self.assertEqual(await self.me_status({'token': again.json()['accessToken']}), 200)

    async def test_default_anonymous_preference(self):
        me = await self.client.get('/api/auth/me', headers=self.auth(self.me))
        self.assertFalse(me.json()['user']['defaultAnonymous'])
        res = await self.client.post('/api/account/preferences', json={'defaultAnonymous': True}, headers=self.auth(self.me))
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['user']['defaultAnonymous'])
        self.assertTrue((await self.client.get('/api/auth/me', headers=self.auth(self.me))).json()['user']['defaultAnonymous'])
        self.assertEqual((await self.client.post('/api/account/preferences', json={'defaultAnonymous': 'yes'},
                                                  headers=self.auth(self.me))).status_code, 422)

    async def test_delete_blocks_live_broadcast_then_leaves_withdrawn_name(self):
        mine_post, other_post = await self.seed_content()
        await self.client.post(f'/api/board/posts/{other_post}/like', headers=self.auth(self.me))
        await NotificationStore(self.redis).create(self.me['id'], 'comment', '알림', '내용')

        self.assertEqual((await self.client.request('DELETE', '/api/account', json={'password': 'nope'},
                                                    headers=self.auth(self.me))).status_code, 403)
        await self.redis.set(f'backend:{self.sid}:station', json.dumps({
            'station_id': self.sid, 'owner_id': self.me['id'], 'status': 'started', 'created_at': time.time()}))
        live = await self.client.request('DELETE', '/api/account', json={'password': PASSWORD}, headers=self.auth(self.me))
        self.assertEqual(live.status_code, 409)
        self.assertEqual(live.json()['detail']['station_id'], self.sid)
        self.assertEqual((await self.names())[0][mine_post], self.me['name'])  # 막혔으면 아무것도 바꾸지 않는다

        await self.redis.set(f'backend:{self.sid}:station', json.dumps({
            'station_id': self.sid, 'owner_id': self.me['id'], 'status': 'ended', 'created_at': time.time()}))
        res = await self.client.request('DELETE', '/api/account', json={'password': PASSWORD}, headers=self.auth(self.me))
        self.assertEqual(res.status_code, 204, res.text)
        posts, comments, requests = await self.names()
        self.assertEqual(posts, {mine_post: WITHDRAWN_NAME, other_post: self.other['name']})
        self.assertEqual(comments, [self.other['name'], WITHDRAWN_NAME, self.other['name']])
        self.assertEqual(requests, {'r_named': WITHDRAWN_NAME, 'r_anon': None, 'r_song': WITHDRAWN_NAME,
                                    'r_other': self.other['name']})
        self.assertEqual(await self.redis.scard(f'backend:board:post-likes:{other_post}'), 0)
        self.assertEqual(await self.redis.exists(f'backend:notifications:{self.me["id"]}'), 0)
        self.assertIsNone(await self.redis.hget('backend:users', self.me['email']))
        self.assertEqual((await self.client.get('/api/auth/me', headers=self.auth(self.me))).status_code, 401)
        self.assertEqual((await self.client.post('/api/auth/login', json={
            'email': self.me['email'], 'password': PASSWORD})).status_code, 401)
        reuse = await self.client.post('/api/auth/signup', json={
            'email': self.me['email'], 'password': PASSWORD, 'name': self.me['name']})
        self.assertEqual(reuse.status_code, 201)  # 이메일과 닉네임을 다시 쓸 수 있다

    async def test_guest_and_admin_have_no_account_settings(self):
        guest = (await self.client.post('/api/auth/guest', json={'name': '손님' + self.tag})).json()
        admin = (await self.client.post('/api/auth/admin', json={'pin': '9999'})).json()
        self.emails += [guest['user']['email'], admin['user']['email']]
        for who in (guest, admin):
            headers = {'Authorization': f'Bearer {who["accessToken"]}'}
            for method, path, body in [('POST', '/api/account/name', {'name': '바꿈' + self.tag}),
                                       ('POST', '/api/account/password', {'currentPassword': 'x', 'newPassword': 'newpass1234'}),
                                       ('POST', '/api/account/preferences', {'defaultAnonymous': True}),
                                       ('DELETE', '/api/account', {'password': 'x'})]:
                with self.subTest(who=who['user']['name'], path=path):
                    res = await self.client.request(method, path, json=body, headers=headers)
                    self.assertEqual(res.status_code, 403)
