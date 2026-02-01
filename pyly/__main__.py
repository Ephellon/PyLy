# # # # # # # # # # # # # # # # # #
#      _____       _              #
#     |  __ \     | |             #
#     | |__) |   _| |    _   _    #
#     |  ___/ | | | |   | | | |   #
#     | |   | |_| | |___| |_| |   #
#     |_|    \__, |______\__, |   #
#             __/ |       __/ |   #
#            |___/       |___/    #
#       ./pyly/__main__.py       #
# # # # # # # # # # # # # # # # # #

from .cli import main
from .console_ui import LiveStatus, banner, ok, err, format_duration, RollingETA

live = LiveStatus(enabled=True)

raise SystemExit(main())
