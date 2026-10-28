# Changelog

## 0.2 (2026-10-28)

- Continue Video: the source video is saved as its own clip, `w00.mp4`, so the window clips hold only the new part;
  the joined video is the source plus the new windows. Also done when the continuation fits in one window.

## 0.1 (2026-10-20)

- First release: each sliding window saved as its own clip in `outputs/window_clips`, with its part of the audio,
  and the clips joined into the normal output at the end without re-encoding (optional). After Early Stop or Abort,
  the windows finished so far are joined.
- With *Keep intermediate sliding windows* on, the last window is also in the gallery on its own, before the joined
  video.
