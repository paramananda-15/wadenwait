python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
if (-not $env:PERCEPTION_MODE) { $env:PERCEPTION_MODE="stub" }
python run.py
