"""Local feature preview: python -m feature_word_meaning --port 8765."""

import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    from .api import create_app
    import uvicorn
    uvicorn.run(create_app(args.data_dir), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
