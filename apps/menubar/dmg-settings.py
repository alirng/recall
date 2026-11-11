# The window Recall's .dmg opens to, for dmgbuild. release.sh passes app and background.
import os.path

application = defines["app"]  # noqa: F821 (dmgbuild provides defines)
appname = os.path.basename(application)

format = "UDZO"
files = [application]
symlinks = {"Applications": "/Applications"}
hide_extension = [appname]

background = defines["background"]  # noqa: F821
window_rect = ((200, 140), (640, 400))
default_view = "icon-view"
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
icon_size = 112
text_size = 13
icon_locations = {appname: (170, 190), "Applications": (470, 190)}
