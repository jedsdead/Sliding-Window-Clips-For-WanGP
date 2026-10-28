"""Sliding Window Clips: save each sliding window as its own clip (see clips.py).

Two settings, in the Sliding Window tab of the generation page, saved for every model:
* Save sliding windows as separate clips: each window is encoded on its own, into outputs/window_clips/<generation>/.
* Also join them at the end: after the last window the clips are joined, without re-encoding, into the normal output.
"""

import gradio as gr

from shared.utils.plugins import WAN2GPPlugin

from . import clips
from . import settings
from .clips import LOG_PREFIX

INSERT_TARGET = "sliding_window_trim_first_frames"


class SlidingWindowClipsPlugin(WAN2GPPlugin):
    def __init__(self):
        super().__init__()
        self.name = "Sliding Window Clips"
        self.version = "0.2"
        self.description = ("Saves each sliding window as its own clip instead of re-encoding the whole video after every "
                            "window, and joins the clips at the end without re-encoding.")
        self._ui_built = False
        settings.load()

    def setup_ui(self):
        self.request_component(INSERT_TARGET)
        clips.install()

    def post_ui_setup(self, components):
        if self._ui_built or components.get(INSERT_TARGET) is None:  # also called for the Edit tab: once is enough
            return {}
        self._ui_built = True
        clips.install()

        def build():
            with gr.Row(elem_id="sliding_window_clips_row") as row:
                enabled = gr.Checkbox(value=settings.get()["enabled"], label="Save sliding windows as separate clips",
                                      info="Each window is encoded on its own, into a folder per generation in outputs/window_clips, "
                                           "instead of re-encoding the whole video after every window.")
                join = gr.Checkbox(value=settings.get()["join"], label="Also join them at the end",
                                   info="After the last window the clips are joined into the normal output, without "
                                        "re-encoding. Off: the output is only the last window.",
                                   interactive=settings.get()["enabled"])

            def store_enabled(value):
                settings.set_value("enabled", bool(value))
                print(f"{LOG_PREFIX} separate clips {'on' if value else 'off'}")
                return gr.update(interactive=bool(value))

            def store_join(value):
                settings.set_value("join", bool(value))
                print(f"{LOG_PREFIX} joining at the end {'on' if value else 'off'}")

            enabled.change(store_enabled, inputs=enabled, outputs=join, show_progress="hidden", queue=False)
            join.change(store_join, inputs=join, outputs=None, show_progress="hidden", queue=False)
            return row

        self.insert_after(INSERT_TARGET, build)
        return {}
