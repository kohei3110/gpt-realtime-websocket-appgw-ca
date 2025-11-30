
## Sideband Architecture Demo (WebRTC + WebSocket Session Separation)

This repository includes a demonstration of OpenAI's [sideband server controls](https://platform.openai.com/docs/guides/realtime-server-controls) approach, which separates the user-OpenAI connection from the server-OpenAI control channel.

### Architecture

```
┌─────────────┐                      ┌─────────────┐
│    User     │◄─────WebRTC─────────►│   OpenAI    │
│  (Browser)  │   (audio/video)      │  Realtime   │
└─────────────┘                      └─────────────┘
                                           ▲
                                           │
                                      WebSocket
                                     (call_id)
                                           │
                                     ┌─────┴─────┐
                                     │   Server  │
                                     │ (Control) │
                                     └───────────┘
```

### Benefits

- **Low Latency**: Audio streams directly between user and OpenAI via WebRTC, bypassing the server
- **Scalability**: Server does not need to handle audio data, reducing bandwidth and CPU requirements
- **Separation of Concerns**: Server handles business logic, tools, and session management while media flows independently
- **Cost Efficiency**: Less server resources needed for audio processing

### How It Works

1. **Session Creation**: Server creates a session ID for tracking
2. **WebRTC Connection**: User establishes direct WebRTC connection to OpenAI, receiving a `call_id`
3. **Sideband Connection**: Server connects to the SAME OpenAI session using the `call_id` via WebSocket
4. **Parallel Operation**: Both connections share the session - user sends audio, server monitors and controls

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/sideband` | GET | Web UI for sideband demo |
| `/sideband/session` | POST | Create a new sideband session |
| `/sideband/ephemeral-key` | POST | Get ephemeral key for WebRTC |
| `/sideband/offer` | POST | Exchange WebRTC SDP offer |
| `/sideband/control/{session_id}` | WS | Server sideband control WebSocket |
| `/sideband/sessions` | GET | List all active sessions |
| `/sideband/session/{session_id}` | GET | Get session details |

### Testing the Sideband Demo

1. **Environment Setup**: Set `OPENAI_API_KEY` environment variable (not Azure OpenAI - WebRTC sideband requires direct OpenAI API)

```bash
export OPENAI_API_KEY=sk-your-api-key
export OPENAI_REALTIME_MODEL=gpt-realtime
```

2. **Start the Server**:

```bash
python -m uvicorn src.main:app --host 0.0.0.0 --port 8080
```

3. **Access the Demo**: Open `http://localhost:8080/sideband` in your browser

4. **Observe Session Separation**: The demo shows:
   - WebRTC connection status (User ↔ OpenAI)
   - WebSocket connection status (Server ↔ OpenAI)
   - Events flowing through both channels
   - Server logs demonstrating that both connections share the same `call_id`

### Understanding the Logs

When running the demo, observe the server logs for session separation confirmation:

```
============================================================
[SIDEBAND SESSION LOG] 2025-11-30T10:30:15.123456
  Session ID: sideband_abc123def456
  Call ID: rtc_u1_9c6574da8b8a41a18da9308f4ad974ce
  Event: Server WebSocket Connected
  Details: Now BOTH user (WebRTC) and server (WebSocket) are connected to the SAME OpenAI session!
  WebRTC Connected: True
  WebSocket (Server) Connected: True
  Events from OpenAI: 5
  Events to OpenAI: 2
============================================================
```

### Key Implementation Files

- [src/sideband.py](src/sideband.py) - Sideband module with WebRTC and WebSocket handling
- [src/main.py](src/main.py) - Main FastAPI app integrating sideband endpoints

### Limitations

- WebRTC sideband requires direct OpenAI API (not Azure OpenAI)
- Browser must support WebRTC
- Ephemeral keys expire after a short time
