"""Launches the Alloc Platform web service and live console locally."""

import os
import sys

import uvicorn

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))


def main():
    print("=" * 60)
    print("  Adaptive Multi-Tenant Cloud Resource Allocation Platform")
    print("  Local Demonstration Server")
    print("  Access web console at: http://127.0.0.1:8000")
    print("=" * 60)

    uvicorn.run(
        "alloc_platform.api.main:app",
        host="127.0.0.1",
        port=8000,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    main()
