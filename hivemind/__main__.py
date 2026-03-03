"""Allow running Hivemind as: python -m hivemind"""

import asyncio

from .cli import main, print_goodbye

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print_goodbye()
