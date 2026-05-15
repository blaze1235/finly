import os
import logging
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("finly.main")

API_PORT = int(os.getenv("PORT", os.getenv("API_PORT", "8080")))

if __name__ == "__main__":
    log.info(f"Starting Finly on port {API_PORT}")
    uvicorn.run("api.main:app", host="0.0.0.0", port=API_PORT, log_level="info")
