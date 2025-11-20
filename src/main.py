import contextlib
import json
import os
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from openai import AsyncOpenAI

load_dotenv()

app = FastAPI(title="Realtime minimal web")

INDEX_HTML = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Realtime demo</title>
    <style>
      body { font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif; margin: 2rem; }
      form { display: flex; gap: 0.5rem; }
      textarea, pre { width: 100%; min-height: 8rem; }
      #status { margin-top: 1rem; color: #666; }
      #transcript { color: #555; font-size: 0.9rem; }
      #audio { color: #0a6; }
    </style>
  </head>
  <body>
    <h1>Azure OpenAI realtime</h1>
    <form id="chat-form">
      <input id="chat-input" type="text" placeholder="Type a message" required />
      <button type="submit">Send</button>
    </form>
    <pre id="response"></pre>
    <p id="transcript"></p>
    <p id="audio">Audio bytes: 0</p>
    <p id="status"></p>
    <script>
      const statusEl = document.getElementById('status');
      const responseEl = document.getElementById('response');
      const transcriptEl = document.getElementById('transcript');
      const audioEl = document.getElementById('audio');
      const form = document.getElementById('chat-form');
      const input = document.getElementById('chat-input');
      const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
      const socket = new WebSocket(`${protocol}://${location.host}/chat`);
      let audioBytes = 0;

      socket.addEventListener('open', () => {
        statusEl.textContent = 'Connected';
      });

      socket.addEventListener('close', () => {
        statusEl.textContent = 'Disconnected';
      });

      socket.addEventListener('message', (event) => {
        const data = JSON.parse(event.data);
        switch (data.type) {
          case 'text-delta':
            responseEl.textContent += data.value ?? '';
            break;
          case 'text-reset':
            responseEl.textContent = '';
            break;
          case 'transcript-delta':
            transcriptEl.textContent += data.value ?? '';
            break;
          case 'transcript-reset':
            transcriptEl.textContent = '';
            break;
          case 'audio-chunk':
            audioBytes += (data.bytes || 0);
            audioEl.textContent = `Audio bytes: ${audioBytes}`;
            break;
          case 'status':
            statusEl.textContent = data.message;
            break;
          case 'error':
            statusEl.textContent = data.message;
            break;
          default:
            break;
        }
      });

      form.addEventListener('submit', (event) => {
        event.preventDefault();
        if (!input.value.trim()) {
          return;
        }
        audioBytes = 0;
        responseEl.textContent = '';
        transcriptEl.textContent = '';
        socket.send(JSON.stringify({ text: input.value.trim() }));
        input.value = '';
      });
    </script>
  </body>
</html>
"""


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def _require_deployment() -> str:
    value = os.getenv('AZURE_OPENAI_DEPLOYMENT_NAME') or os.getenv('AZURE_OPENAI_DEPLOYMENT')
    if not value:
        raise RuntimeError("Environment variable AZURE_OPENAI_DEPLOYMENT_NAME (or legacy AZURE_OPENAI_DEPLOYMENT) is required")
    return value


def _build_ws_base(endpoint: str) -> str:
    base = endpoint.strip().rstrip('/')
    if base.startswith('https://'):
        base = 'wss://' + base[len('https://'):]
    elif base.startswith('http://'):
        base = 'wss://' + base[len('http://'):]
    return f"{base}/openai/v1"


_client: AsyncOpenAI | None = None
_deployment_name: str | None = None


def _get_client() -> tuple[AsyncOpenAI, str]:
    global _client, _deployment_name
    if _client is None or _deployment_name is None:
        endpoint = _require_env('AZURE_OPENAI_ENDPOINT')
        deployment = _require_deployment()
        api_key = _require_env('AZURE_OPENAI_API_KEY')
        base_url = _build_ws_base(endpoint)
        _client = AsyncOpenAI(websocket_base_url=base_url, api_key=api_key)
        _deployment_name = deployment
    return _client, _deployment_name


async def _relay_to_azure(websocket: WebSocket, user_text: str) -> None:
    client, deployment = _get_client()
    await websocket.send_json({'type': 'status', 'message': 'Connecting to Azure OpenAI...'})
    try:
        async with client.realtime.connect(model=deployment) as connection:
            await connection.session.update(session={
                'instructions': 'You are a helpful assistant. You respond by voice and text.',
                'output_modalities': ['audio'],
                'audio': {
                    'input': {
                        'transcription': {'model': 'whisper-1'},
                        'format': {'type': 'audio/pcm', 'rate': 24000},
                        'turn_detection': {
                            'type': 'server_vad',
                            'threshold': 0.5,
                            'prefix_padding_ms': 300,
                            'silence_duration_ms': 200,
                            'create_responese': True,
                        },
                    },
                    'output': {
                        'voice': 'alloy',
                        'format': {'type': 'audio/pcm', 'rate': 24000},
                    },
                },
            })

            await connection.conversation.item.create(
                item={
                    'type': 'message',
                    'role': 'user',
                    'content': [{'type': 'input_text', 'text': user_text}],
                }
            )
            await websocket.send_json({'type': 'text-reset'})
            await websocket.send_json({'type': 'transcript-reset'})
            await connection.response.create()

            async for event in connection:
                if event.type == 'response.output_text.delta':
                    await websocket.send_json({'type': 'text-delta', 'value': event.delta})
                elif event.type == 'response.output_audio.delta':
                    chunk = event.delta or ''
                    await websocket.send_json({'type': 'audio-chunk', 'value': chunk, 'bytes': len(chunk)})
                elif event.type == 'response.output_audio_transcript.delta':
                    await websocket.send_json({'type': 'transcript-delta', 'value': event.delta})
                elif event.type == 'response.output_text.done':
                    await websocket.send_json({'type': 'status', 'message': 'Response complete'})
                elif event.type == 'response.done':
                    await websocket.send_json({'type': 'status', 'message': 'Model ready'})
                    break
    except Exception as exc:  # pragma: no cover - network failures are environment specific
        await websocket.send_json({'type': 'error', 'message': f'Azure OpenAI error: {exc}'})


@app.get('/', response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


@app.get('/healthz')
async def healthz() -> JSONResponse:
    revision = os.getenv('CONTAINER_APP_REVISION', 'local')
    return JSONResponse({'status': 'ok', 'revision': revision})


@app.websocket('/chat')
async def chat(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload: Any = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({'type': 'error', 'message': 'Messages must be JSON'})
                continue

            user_text = (payload.get('text') or '').strip()
            if not user_text:
                await websocket.send_json({'type': 'error', 'message': 'Provide text input'})
                continue

            await _relay_to_azure(websocket, user_text)
    except WebSocketDisconnect:
        pass
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()