import base64
import contextlib
import json
import os
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from openai import AsyncAzureOpenAI

load_dotenv()

DEFAULT_API_VERSION = "2025-04-01-preview"

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
    <audio id="player" hidden></audio>
    <p id="status"></p>
    <script>
      const SAMPLE_RATE = 24000;
      const statusEl = document.getElementById('status');
      const responseEl = document.getElementById('response');
      const transcriptEl = document.getElementById('transcript');
      const audioEl = document.getElementById('audio');
      const form = document.getElementById('chat-form');
      const input = document.getElementById('chat-input');
      const wsEndpoint = '{{WS_ENDPOINT}}';
      const socket = new WebSocket(wsEndpoint);
      let audioBytes = 0;
      let audioCtx;
      let audioPlayhead = 0;

      function ensureAudioContext() {
        if (!audioCtx) {
          audioCtx = new AudioContext({ sampleRate: SAMPLE_RATE });
        }
        if (audioCtx.state === 'suspended') {
          audioCtx.resume();
        }
        return audioCtx;
      }

      window.addEventListener('click', () => ensureAudioContext(), { once: true });

      function playPcmChunk(base64Chunk) {
        if (!base64Chunk) {
          return;
        }
        const ctx = ensureAudioContext();
        const binary = atob(base64Chunk);
        const buffer = new ArrayBuffer(binary.length);
        const bytes = new Uint8Array(buffer);
        for (let i = 0; i < binary.length; i += 1) {
          bytes[i] = binary.charCodeAt(i);
        }

        const sampleCount = bytes.length / 2;
        const floatSamples = new Float32Array(sampleCount);
        const view = new DataView(buffer);
        for (let i = 0; i < sampleCount; i += 1) {
          const sample = view.getInt16(i * 2, true);
          floatSamples[i] = sample / 32768;
        }

        const audioBuffer = ctx.createBuffer(1, sampleCount, ctx.sampleRate);
        audioBuffer.getChannelData(0).set(floatSamples);
        const source = ctx.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(ctx.destination);
        const now = ctx.currentTime;
        if (audioPlayhead < now) {
          audioPlayhead = now;
        }
        source.start(audioPlayhead);
        audioPlayhead += audioBuffer.duration;
      }

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
            playPcmChunk(data.value);
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


_client: AsyncAzureOpenAI | None = None
_deployment_name: str | None = None


def _get_client() -> tuple[AsyncAzureOpenAI, str]:
    global _client, _deployment_name
    if _client is None or _deployment_name is None:
        endpoint = _require_env('AZURE_OPENAI_ENDPOINT')
        deployment = _require_deployment()
        api_key = _require_env('AZURE_OPENAI_API_KEY')
        api_version = os.getenv('AZURE_OPENAI_API_VERSION', DEFAULT_API_VERSION)
        _client = AsyncAzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint,
            api_version=api_version,
        )
        _deployment_name = deployment
    return _client, _deployment_name


async def _relay_to_azure(websocket: WebSocket, user_text: str) -> None:
    client, deployment = _get_client()
    print(f"Relaying to Azure OpenAI deployment '{deployment}': {user_text}, {client._azure_endpoint}, {client._api_version}")
    await websocket.send_json({'type': 'status', 'message': 'Connecting to Azure OpenAI...'})
    try:
        async with client.realtime.connect(model=deployment) as connection:
            await connection.session.update(session={
              'instructions': 'You are a helpful assistant. You respond by voice and text.',
              'output_modalities': ['text', 'audio'],
              'audio': {
                'input': {
                  'transcription': {'model': 'whisper-1'},
                  'format': {'type': 'audio/pcm', 'rate': 24000},
                  'turn_detection': {
                      'type': 'server_vad',
                      'threshold': 0.5,
                      'prefix_padding_ms': 300,
                      'silence_duration_ms': 200,
                      'create_response': True,
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
              print(f"Received event: {event.type}")  # デバッグ用
              if event.type == 'response.text.delta':
                await websocket.send_json({'type': 'text-delta', 'value': event.delta})
              elif event.type == 'response.audio.delta':
                chunk = event.delta or ''
                if not chunk:
                  continue
                raw_bytes = base64.b64decode(chunk)
                await websocket.send_json({'type': 'audio-chunk', 'value': chunk, 'bytes': len(raw_bytes)})
              elif event.type == 'response.audio_transcript.delta':
                await websocket.send_json({'type': 'transcript-delta', 'value': event.delta})
              elif event.type == 'response.text.done':
                await websocket.send_json({'type': 'status', 'message': 'Response complete'})
              elif event.type == 'response.done':
                await websocket.send_json({'type': 'status', 'message': 'Model ready'})
                break
    except Exception as exc:  # pragma: no cover - network failures are environment specific
        await websocket.send_json({'type': 'error', 'message': f'Azure OpenAI error: {exc}'})


@app.get('/', response_class=HTMLResponse)
async def index() -> HTMLResponse:
    agw_host = os.getenv('APPLICATION_GATEWAY_HOST', '')
    if agw_host:
        protocol = 'wss' if agw_host.startswith('https://') else 'ws'
        agw_host = agw_host.replace('https://', '').replace('http://', '')
        ws_endpoint = f'{protocol}://{agw_host}/chat'
    else:
        ws_endpoint = '${protocol}://${location.host}/chat'
        html_content = INDEX_HTML.replace(
            "const wsEndpoint = '{{WS_ENDPOINT}}';",
            "const protocol = location.protocol === 'https:' ? 'wss' : 'ws';\n      const wsEndpoint = `${protocol}://${location.host}/chat`;"
        )
        return HTMLResponse(html_content)
    
    html_content = INDEX_HTML.replace('{{WS_ENDPOINT}}', ws_endpoint)
    print(f"Serving index with WS endpoint: {ws_endpoint}")
    return HTMLResponse(html_content)


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
            print(f"Received from client: {raw}")
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