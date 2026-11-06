import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from gui import App  # noqa: E402


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
