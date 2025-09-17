# digitization-toolkit-backend

To run the FastAPI backend server:

```bash
python3.11 -m venv .venv # Windows (PowerShell): python -m venv .venv
source .venv/bin/activate  # Windows (PowerShell): .venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 # Command just for testing
```

Test: Open your browser and navigate to `http://localhost:8000` to see the health check response -> `{"status": "ok"}`