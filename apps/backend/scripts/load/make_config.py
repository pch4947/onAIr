"""Write an engine config for the isolated load test: dummy LLM/TTS/embedder, own files, own port.

Usage (cwd = apps/engine): make_config.py WORK_DIR API_PORT REDIS_URL
"""
import sys
from pathlib import Path

import yaml

work, port, redis_url = Path(sys.argv[1]).resolve(), int(sys.argv[2]), sys.argv[3]
raw = yaml.safe_load(Path('config/station.example.yaml').read_text(encoding='utf-8'))
pipe = raw['pipeline']
pipe.update(llm='dummy', tts='dummy', embedder='dummy',
            tts_cache_dir=str(work / 'tts_cache'), talk_bank_cache=str(work / 'talk_bank.sqlite'),
            topic_usage=str(work / 'topic_usage.sqlite'))
for key in ('llm_model', 'llm_base_url', 'llm_reasoning_effort', 'tts_voice'):
    pipe.pop(key, None)
raw['transport'].update(kind='redis', redis_url=redis_url, audio_dir=str(work / 'audio'))
raw['telemetry'] = {'sqlite_path': str(work / 'engine_metrics.sqlite')}
raw['api'] = dict(raw.get('api') or {}, host='127.0.0.1', port=port)
(work / 'engine.yaml').write_text(yaml.safe_dump(raw, allow_unicode=True), encoding='utf-8')
print(work / 'engine.yaml')
