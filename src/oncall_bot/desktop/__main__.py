import asyncio
import sys


def main():
    if "--engine" in sys.argv or "--turn" in sys.argv:
        sys.stdout.reconfigure(encoding="utf-8")
    if "--engine" in sys.argv:
        from .engine import serve

        try:
            asyncio.run(serve())
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
        return 0
    if "--turn" in sys.argv:
        from .turn import main as turn

        return turn()
    from .gui import run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
