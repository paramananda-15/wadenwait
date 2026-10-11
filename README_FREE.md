# Wade or Wait — ₹0 Local Edition

This version removes AWS, OpenRouter, S3, DynamoDB, Lambda and API Gateway. It runs the complete demo on one computer using FastAPI + SQLite. AI mode uses a local Ollama vision model.

## 1. Install Python
Python 3.10+ recommended.

## 2. Install Ollama
Install Ollama from https://ollama.com/ and make sure `ollama` is available in your terminal.

For AI mode, download a vision-capable model, for example:

    ollama pull qwen2.5vl:7b

Model availability depends on your Ollama installation. You can use another vision model by setting `OLLAMA_MODEL`.

## 3. Run without AI first

    ./run_free.sh

The first run creates `.venv`, installs dependencies and starts the server.

Open:

    http://127.0.0.1:8000

Stub mode is deliberately deterministic and needs no API key.

## 4. Turn on local AI
Stop the server and run:

    export PERCEPTION_MODE=ollama
    export OLLAMA_MODEL=qwen2.5vl:7b
    ./run_free.sh

On Windows PowerShell:

    $env:PERCEPTION_MODE="ollama"
    $env:OLLAMA_MODEL="qwen2.5vl:7b"
    .\run_free.ps1

## 5. Phone demo
`localhost` is a secure browser context, so camera/GPS work on the same computer. To use a phone over LAN, bind the server to `0.0.0.0` and use an HTTPS tunnel such as Cloudflare Tunnel. Plain LAN HTTP may be blocked by browser secure-context rules for GPS/camera.

## Safety
The geometry and vehicle limits in this repository are illustrative demo values. They are NOT validated engineering guidance and must not be used for real flood-crossing decisions.
