"""Open N broadcasts on an isolated backend and measure whether HLS keeps up.

Usage: loadtest.py BASE_URL CHANNELS SECONDS OUT_DIR [--watch PID,PID] [--formats talk,music]
"""
import argparse
import asyncio
import json
import os
import secrets
import statistics
import sys
import time

import httpx

PERSONA = {'style': {'formality': 'polite', 'energy': 'low', 'humor': 'rare'},
           'voice': 'dummy-feminine', 'dj_name': 'DJ', 'concept': 'load test broadcast',
           'tone': 'calm and polite', 'examples': ['Hello there.', 'Nice to see you.', 'Take a rest.'],
           'music_taste': ['k-pop']}
MIN_AVAILABLE_MB = 110


def meminfo():
    values = {}
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                name, _, rest = line.partition(':')
                values[name] = int(rest.split()[0]) // 1024
    except OSError:
        return None
    return values


def cpu_times():
    try:
        with open('/proc/stat') as f:
            parts = [int(x) for x in f.readline().split()[1:]]
    except OSError:
        return None
    idle = parts[3] + parts[4]
    return sum(parts), idle, parts[4]


def rss_mb(pid):
    try:
        with open(f'/proc/{pid}/status') as f:
            for line in f:
                if line.startswith('VmRSS'):
                    return int(line.split()[1]) // 1024
    except OSError:
        return None


def proc_cpu(pid):
    try:
        with open(f'/proc/{pid}/stat') as f:
            parts = f.read().rsplit(')', 1)[1].split()
        return (int(parts[11]) + int(parts[12])) / os.sysconf('SC_CLK_TCK')
    except (OSError, IndexError, ValueError):
        return None


class Channel:
    def __init__(self, index, fmt):
        self.index, self.format = index, fmt
        self.token = self.station_id = self.created_at = None
        self.created_wall = self.started_after = self.first_playlist_after = None
        self.status = None
        self.end = None  # media sequence + segment count
        self.last_advance = None
        self.gaps = []
        self.segments = 0
        self.errors = 0
        self.segment_ms = []

    def summary(self, segment_seconds):
        gaps = sorted(self.gaps)
        return dict(channel=self.station_id, format=self.format, status=self.status,
                    started_after_s=self.started_after, first_playlist_after_s=self.first_playlist_after,
                    segments=self.segments, errors=self.errors,
                    gap_max_s=round(gaps[-1], 2) if gaps else None,
                    gap_p95_s=round(gaps[int(len(gaps) * 0.95) - 1], 2) if len(gaps) >= 20 else None,
                    stalls_over_6s=sum(1 for gap in gaps if gap > 3 * segment_seconds),
                    segment_fetch_ms_p95=round(sorted(self.segment_ms)[int(len(self.segment_ms) * 0.95) - 1])
                    if len(self.segment_ms) >= 20 else None)


async def open_channel(client, channel, minutes):
    name = 'load' + secrets.token_hex(4)
    response = await client.post('/api/auth/signup', json=dict(
        email=name + '@example.com', password=secrets.token_urlsafe(18), name=name))
    response.raise_for_status()
    channel.token = response.json()['accessToken']
    body = dict(broadcast_minutes=minutes, persona=PERSONA, topic='load test', format=channel.format)
    response = await client.post('/api/stations', json=body, headers={
        'Authorization': 'Bearer ' + channel.token, 'Idempotency-Key': 'load-' + name})
    if response.status_code != 202:
        raise RuntimeError(f'create {channel.index}: {response.status_code} {response.text[:200]}')
    station = response.json()['station']
    channel.station_id, channel.created_at = station['station_id'], station['created_at']
    channel.created_wall = time.monotonic()
    channel.status = station['status']


async def watch(client, channel, deadline):
    auth = {'Authorization': 'Bearer ' + channel.token}
    while time.monotonic() < deadline:
        tick = time.monotonic()
        try:
            if channel.started_after is None:
                response = await client.get(f'/api/stations/{channel.station_id}', headers=auth)
                if response.status_code == 200:
                    channel.status = response.json()['station']['status']
                    if channel.status == 'started':
                        channel.started_after = round(time.monotonic() - channel.created_wall, 1)
            response = await client.get(f'/hls/{channel.station_id}/index.m3u8')
            if response.status_code == 200:
                lines = response.text.splitlines()
                names = [line for line in lines if line and not line.startswith('#')]
                sequence = next((int(line.split(':')[1]) for line in lines
                                 if line.startswith('#EXT-X-MEDIA-SEQUENCE')), 0)
                end = sequence + len(names)
                now = time.monotonic()
                if channel.first_playlist_after is None and names:
                    channel.first_playlist_after = round(now - channel.created_wall, 1)
                if channel.end is None or end > channel.end:
                    if channel.last_advance is not None:
                        channel.gaps.append(now - channel.last_advance)
                    channel.segments += (end - channel.end) if channel.end is not None else len(names)
                    channel.end, channel.last_advance = end, now
                    if names:  # one listener per channel: fetch the newest segment
                        began = time.monotonic()
                        got = await client.get(f'/hls/{channel.station_id}/{names[-1]}')
                        if got.status_code == 200:
                            channel.segment_ms.append((time.monotonic() - began) * 1000)
                        else:
                            channel.errors += 1
            elif response.status_code not in (404, 503) or channel.first_playlist_after is not None:
                channel.errors += 1
        except httpx.HTTPError:
            channel.errors += 1
        await asyncio.sleep(max(0.05, 0.5 - (time.monotonic() - tick)))
    if channel.last_advance is not None:  # a stall still running when the test ends
        channel.gaps.append(time.monotonic() - channel.last_advance)


async def sample_system(samples, pids, deadline, abort):
    previous, previous_proc = cpu_times(), {pid: proc_cpu(pid) for pid in pids}
    stamp = time.monotonic()
    low = 0
    while time.monotonic() < deadline and not abort.is_set():
        await asyncio.sleep(5)
        memory, current = meminfo(), cpu_times()
        if memory is None or current is None or previous is None:
            return
        now = time.monotonic()
        total = current[0] - previous[0] or 1
        row = dict(t=round(now - samples['began']), cpu_busy_pct=round(100 * (1 - (current[1] - previous[1]) / total), 1),
                   iowait_pct=round(100 * (current[2] - previous[2]) / total, 1),
                   load1=float(open('/proc/loadavg').read().split()[0]),
                   mem_available_mb=memory['MemAvailable'], swap_used_mb=memory['SwapTotal'] - memory['SwapFree'])
        for pid in pids:
            used = proc_cpu(pid)
            row[f'rss_{pid}'] = rss_mb(pid)
            if used is not None and previous_proc.get(pid) is not None:
                row[f'cpu_{pid}'] = round(100 * (used - previous_proc[pid]) / (now - stamp), 1)
            previous_proc[pid] = used
        samples['rows'].append(row)
        previous, stamp = current, now
        low = low + 1 if memory['MemAvailable'] < MIN_AVAILABLE_MB else 0
        if low >= 2:
            samples['aborted'] = f'MemAvailable below {MIN_AVAILABLE_MB}MB'
            abort.set()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('base')
    parser.add_argument('channels', type=int)
    parser.add_argument('seconds', type=int)
    parser.add_argument('out')
    parser.add_argument('--watch', default='')
    parser.add_argument('--formats', default='talk,music')
    parser.add_argument('--segment-seconds', type=float, default=2)
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)
    pids = [int(pid) for pid in args.watch.split(',') if pid]
    formats = args.formats.split(',')
    channels = [Channel(i, formats[i % len(formats)]) for i in range(args.channels)]
    samples = dict(began=time.monotonic(), rows=[])
    abort = asyncio.Event()
    async with httpx.AsyncClient(base_url=args.base, timeout=10) as client:
        idle = dict(mem=meminfo())
        for channel in channels:  # one at a time: channel choice is serialised in the backend anyway
            await open_channel(client, channel, 30)
        deadline = time.monotonic() + args.seconds
        watchers = [asyncio.create_task(watch(client, channel, deadline)) for channel in channels]
        sampler = asyncio.create_task(sample_system(samples, pids, deadline, abort))
        stopper = asyncio.create_task(abort.wait())
        await asyncio.wait([asyncio.gather(*watchers), stopper], return_when=asyncio.FIRST_COMPLETED)
        for task in watchers + [sampler, stopper]:
            task.cancel()
        await asyncio.gather(*watchers, sampler, stopper, return_exceptions=True)
        stopped = []
        for channel in channels:
            try:
                response = await client.post(f'/api/stations/{channel.station_id}/stop',
                                             json={'created_at': channel.created_at},
                                             headers={'Authorization': 'Bearer ' + channel.token})
                stopped.append(response.status_code)
            except httpx.HTTPError as exc:
                stopped.append(repr(exc))
    rows = samples['rows']
    result = dict(channels=[channel.summary(args.segment_seconds) for channel in channels],
                  stop_status=stopped, aborted=samples.get('aborted'), seconds=args.seconds,
                  idle_mem_available_mb=(idle['mem'] or {}).get('MemAvailable'))
    if rows:
        result['system'] = dict(
            cpu_busy_avg=round(statistics.mean(row['cpu_busy_pct'] for row in rows), 1),
            cpu_busy_max=max(row['cpu_busy_pct'] for row in rows),
            load1_max=max(row['load1'] for row in rows),
            mem_available_min_mb=min(row['mem_available_mb'] for row in rows),
            swap_used_max_mb=max(row['swap_used_mb'] for row in rows),
            **{key: max(row.get(key) or 0 for row in rows) for key in rows[-1] if key.startswith(('rss_', 'cpu_')) and key != 'cpu_busy_pct'})
    with open(os.path.join(args.out, 'timeline.json'), 'w') as f:
        json.dump(rows, f)
    with open(os.path.join(args.out, 'result.json'), 'w') as f:
        json.dump(result, f, indent=1)
    print(json.dumps(result, indent=1))


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
