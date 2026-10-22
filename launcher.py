"""GUI entry point: preserve startup errors even when no terminal is attached."""
import html
import os
from pathlib import Path
import sys
import traceback
import webbrowser


def main():
    try:
        import server
        server.main()
        return 0
    except Exception as error:
        details = traceback.format_exc()
        if sys.stderr:
            print(details, file=sys.stderr)
        try:
            from platform_runtime import user_data_dir
            folder = Path(os.environ.get("DASHBOARD_DATA_DIR") or user_data_dir())
            folder.mkdir(parents=True, exist_ok=True)
            log = folder / "startup-error.log"
            log.write_text(details, encoding="utf-8")
            page = folder / "startup-error.html"
            page.write_text(
                '<!doctype html><html lang="en"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                '<title>Dev Services startup error</title>'
                '<style>body{max-width:760px;margin:64px auto;padding:24px;font:16px/1.6 system-ui}'
                'pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f4f6;padding:16px}</style>'
                '<h1>Dev Services could not start</h1><p>' + html.escape(str(error)) + '</p>'
                '<p>Your services have not been stopped. Close an older dashboard instance or choose '
                'another port with DASHBOARD_PORT, then open Dev Services again.</p>'
                '<p>Technical details are saved in:</p><pre>' + html.escape(str(log)) + '</pre></html>',
                encoding="utf-8")
            if "--no-browser" not in sys.argv:
                webbrowser.open(page.resolve().as_uri())
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
