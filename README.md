# Sliding Window Clips for Wan2GP

A Wan2GP plugin for long, sliding-window generations. After each window Wan2GP normally re-encodes the **whole**
video so far, so every save takes longer than the last. With this plugin each window is encoded on its own, and
saves stay fast however long the video gets.

## What you get

- **One clip per window**, with its part of the audio, in `outputs/window_clips/<generation>/`: `w01.mp4`,
  `w02.mp4`... Placed back to back they are the normal output, frame for frame (the overlap frames are already
  removed), ready to edit in post-production.
- **The joined video** in your outputs folder as usual: after the last window the clips are joined without
  re-encoding, which takes seconds. Untick **Also join them at the end** to skip it (the output is then only the
  last window; the clips are all in the folder).

With Wan2GP's *Keep intermediate sliding windows* setting on, the gallery shows every window on its own, the last
one included (saved next to the joined video as `..._w13.mp4`), then the joined video.

Both options are in the **Sliding Window** tab of the generation page.

Generations with a single window are left to Wan2GP, and so are HDR outputs and soundtrack generators that need the
whole video (MMAudio...). Early Stop and Abort work too: the windows finished so far are joined, and the gallery
shows that video.

## Install

In Wan2GP, open the **Plugins** tab, paste this repository's URL under *Install New Plugin*, install,
enable it and restart Wan2GP.

## License

MIT
