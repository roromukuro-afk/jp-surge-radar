"""サイトをローカルで起動する(.env を読んでから uvicorn を起動)。"""
import _boot  # noqa: F401
import uvicorn

if __name__ == "__main__":
    uvicorn.run("surge_radar.web.app_vercel:app", host="127.0.0.1", port=8012)
