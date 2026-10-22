"""Save each sliding window as its own clip, instead of re-encoding the whole video after every window.

How WanGP saves a sliding-window generation: after each window it appends the window's new frames to the frames kept
in memory (a list with one entry per window) and encodes the *whole* list again into a new file, then adds the audio
with a second, fast step (the video stream is only copied). The encode grows with every window.

This module replaces two WanGP functions (WanGP looks them up by name each time, so no WanGP file is changed):
* save_video: when it is called by the generator with that per-window list, only the newest window is encoded, into
  outputs/window_clips/<generation>/w01.mp4, w02.mp4... Played back to back the clips are the normal output, frame for
  frame (the overlap frames are already cut off by WanGP).
* combine_and_concatenate_video_with_audio_tracks: the audio added to a clip is cut to that clip's time span (newer
  WanGP versions keep a little audio past the last frame, audio_tail_seconds: that is left to the joined video).
If the generation is aborted, the finished windows are joined into its latest output file the same way (the gallery
shows it). After the last window, the clips are joined into the normal output file without re-encoding (the audio is added the
way WanGP does it), or, with joining off, the last clip is copied there. Before that, each window's normal output
file is a copy of its clip, so the gallery, the metadata and "keep intermediate sliding windows" work as usual.

Anything else is left to WanGP: single-window generations, HDR, and soundtrack generators that need the whole video
(MMAudio...). If a clip or the join fails, WanGP's own full encode is used for that file.
"""

import functools
import inspect
import os
import shutil
import subprocess
import sys
import tempfile
import threading

from . import settings

LOG_PREFIX = "[Sliding Window Clips]"
CLIPS_FOLDER = "window_clips"
_WRAPPED_FLAG = "_sliding_window_clips_wrapped"

_lock = threading.Lock()
_run = {}       # the generation in progress: key, folder, clips
_pending = {}   # temporary video file -> what the audio step needs for that clip
_final_windows = {}  # joined output file -> the last window's own file, added to the gallery just before it


def _ffmpeg():
    try:
        from shared.utils.audio_video import _ffmpeg_binary
        return _ffmpeg_binary()
    except Exception:
        return "ffmpeg"


def _generator_locals(limit=4):
    """Local variables of WanGP's generate_media, seen from a function it calls (or None)."""
    frame = sys._getframe(2)
    for _ in range(limit):
        if frame is None:
            return None
        if frame.f_code.co_name == "generate_media" and "frames_already_processed" in frame.f_locals:
            return frame.f_locals
        frame = frame.f_back
    return None


def _is_last_window(local):
    gen = local.get("gen") or {}
    if local.get("stop_current_sample"):  # Early Stop: this window ends the video
        return True
    return int(local.get("window_no") or 0) >= int(local.get("total_windows") or 0) and not gen.get("extra_windows", 0)


def _run_for(local, chunks, container):
    """The clips folder of this generation: created at its first window, found again at the next ones."""
    key = (id(local.get("frames_already_processed")), local.get("seed"))
    with _lock:
        if _run.get("key") != key or len(chunks) != _run.get("count", 0) + 1:
            stem = os.path.splitext(str(local.get("file_name") or "video"))[0]
            root = os.path.join(str(local.get("output_dir") or "outputs"), CLIPS_FOLDER)
            folder = os.path.join(root, stem)
            suffix = 1
            while os.path.exists(folder):
                suffix += 1
                folder = os.path.join(root, f"{stem}_{suffix}")
            _run.clear()
            _run.update(key=key, folder=folder, clips=[], count=0, gen=local.get("gen"))
        _run["count"] = len(chunks)
        os.makedirs(_run["folder"], exist_ok=True)
        return dict(_run, clips=_run["clips"]), os.path.join(_run["folder"], f"w{len(chunks):02d}.{container}")


def _clip_done(clip_path, last, output_path=None):
    with _lock:
        _run.setdefault("clips", []).append(clip_path)
        _run["last_output"] = output_path
        clips = list(_run["clips"])
        if last:
            _run.clear()
    return clips


def _keeps_windows():
    """WanGP's "keep intermediate sliding windows": every window has its own video in the gallery."""
    config = getattr(sys.modules.get("__main__"), "server_config", None) or {}
    return bool(config.get("keep_intermediate_sliding_windows", 1))


def _add_final_window(joined_path, clip_path, window):
    """The last window gets its own gallery video too, like the windows before it (the joined video takes its place
    otherwise). A copy of its clip, next to the joined video; recorded in the gallery by record_file_metadata."""
    if not _keeps_windows():
        return
    try:
        root, extension = os.path.splitext(joined_path)
        window_path = f"{root}_w{window:02d}{extension}"
        shutil.copyfile(clip_path, window_path)
        with _lock:
            _final_windows[os.path.abspath(joined_path)] = window_path
    except Exception as error:
        print(f"{LOG_PREFIX} the last window was not added to the gallery on its own: {error!r}")


def _concat(clips, output_path, video_only=False):
    """Join clips without re-encoding. True if it worked."""
    if len(clips) == 1 and not video_only:
        shutil.copyfile(clips[0], output_path)
        return True
    handle, list_path = tempfile.mkstemp(suffix=".txt", prefix="window_clips_")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as listing:
            for clip in clips:
                listing.write("file '" + os.path.abspath(clip).replace("'", "'\\''") + "'\n")
        cmd = [_ffmpeg(), "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", list_path,
               "-map", "0:v", *([] if video_only else ["-map", "0:a?"]), "-c", "copy", output_path]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            print(f"{LOG_PREFIX} joining the clips failed: {result.stderr.strip()[-400:]}")
            return False
        return True
    finally:
        os.remove(list_path)


def _audio_from(track, start, folder):
    """The part of an audio file from start (seconds), as a temporary WAV (None if it could not be made)."""
    handle, path = tempfile.mkstemp(suffix=".wav", prefix="clip_audio_", dir=folder)
    os.close(handle)
    cmd = [_ffmpeg(), "-y", "-v", "error", "-ss", f"{max(0.0, start):.6f}", "-i", track, "-vn", "-c:a", "pcm_s24le", path]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 or not os.path.getsize(path):
        os.remove(path)
        return None
    return path


def _silence(duration, rate, folder):
    handle, path = tempfile.mkstemp(suffix=".wav", prefix="clip_audio_", dir=folder)
    os.close(handle)
    cmd = [_ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i", f"anullsrc=r={int(rate)}:cl=stereo",
           "-t", f"{max(0.04, duration):.6f}", "-c:a", "pcm_s24le", path]
    subprocess.run(cmd, capture_output=True, text=True)
    return path


def _wrap_save_video(original):
    @functools.wraps(original)
    def save_video(*args, **kwargs):
        tensor = kwargs.get("tensor", args[0] if args else None)
        if not settings.get()["enabled"] or not isinstance(tensor, list) or not tensor:
            return original(*args, **kwargs)
        local = _generator_locals()
        if (local is None or tensor is not local.get("frames_already_processed") or not local.get("sliding_window")
                or local.get("any_soundtrack_processor") or local.get("sample_is_hdr")):
            return original(*args, **kwargs)
        last = _is_last_window(local)
        if len(tensor) == 1 and last:  # a single window: nothing to split
            return original(*args, **kwargs)
        save_file = kwargs.get("save_file", args[1] if len(args) > 1 else None)
        container = kwargs.get("container", "mp4")
        fps = kwargs.get("fps", 30)
        options = {key: value for key, value in kwargs.items() if key not in ("tensor", "save_file")}
        try:
            run, clip_path = _run_for(local, tensor, container)
            original(tensor=[tensor[-1]], save_file=clip_path, **options)
        except Exception as error:
            print(f"{LOG_PREFIX} could not save the window as a clip, WanGP saves the whole video: {error!r}")
            return original(*args, **kwargs)
        frames_before = sum(int(chunk.shape[1]) for chunk in tensor[:-1])
        window = len(tensor)
        print(f"{LOG_PREFIX} window {window} saved as {os.path.relpath(clip_path, os.path.dirname(os.path.dirname(run['folder'])))}")

        if save_file is not None and save_file == local.get("save_path_tmp"):
            # Audio is added next (combine_and_concatenate_video_with_audio_tracks): it builds the clip from this file
            shutil.move(clip_path, save_file)
            with _lock:
                _pending[os.path.abspath(save_file)] = dict(clip=clip_path, start=frames_before / fps,
                                                            duration=int(tensor[-1].shape[1]) / fps, last=last,
                                                            frames=tensor, options=options, window=window)
            return save_file

        clips = _clip_done(clip_path, last, save_file)
        if last and settings.get()["join"]:
            if _concat(clips, save_file):
                print(f"{LOG_PREFIX} {len(clips)} clips joined into {os.path.basename(save_file)}")
                _add_final_window(save_file, clip_path, window)
            else:
                print(f"{LOG_PREFIX} WanGP saves the whole video instead")
                return original(*args, **kwargs)
        else:
            shutil.copyfile(clip_path, save_file)
        return save_file

    setattr(save_video, _WRAPPED_FLAG, True)
    return save_video


def _wrap_combine(original, save_video_original):
    @functools.wraps(original)
    def combine(save_path_tmp, video_path, source_audio_tracks, new_audio_tracks, source_audio_duration, audio_sampling_rate,
                *args, **kwargs):
        # WanGP's names: save_path_tmp is the file written, video_path the video read
        with _lock:
            clip = _pending.pop(os.path.abspath(video_path), None)
        if clip is None:
            return original(save_path_tmp, video_path, source_audio_tracks, new_audio_tracks, source_audio_duration,
                            audio_sampling_rate, *args, **kwargs)
        folder = os.path.dirname(clip["clip"])
        new_audio_from_start = kwargs.get("new_audio_from_start", args[0] if args else False)
        temporary = []
        try:
            if clip["start"] <= 0:  # the first window starts the video: WanGP's audio as is, cut at the clip's end
                first_kwargs = {key: value for key, value in kwargs.items() if key != "audio_tail_seconds"}
                original(clip["clip"], video_path, source_audio_tracks, new_audio_tracks, source_audio_duration,
                         audio_sampling_rate, *args, **first_kwargs)
            else:
                # Position of the clip's start in the new audio tracks (after a source video's own audio, if any)
                offset = clip["start"] - (source_audio_duration if source_audio_duration and new_audio_from_start else 0)
                tracks = [_audio_from(track, offset, folder) for track in (new_audio_tracks or [])]
                tracks = [track if track else _silence(clip["duration"], audio_sampling_rate, folder) for track in tracks]
                if not tracks:
                    tracks = [_silence(clip["duration"], audio_sampling_rate, folder)]
                temporary += tracks
                options = {key: value for key, value in kwargs.items() if key in ("audio_codec_key", "verbose", "pad_audio_to_video")}
                original(clip["clip"], video_path, [], tracks, 0, audio_sampling_rate, new_audio_from_start=True, **options)
        except Exception as error:
            print(f"{LOG_PREFIX} could not add the audio to the clip: {error!r}")
            if not os.path.isfile(clip["clip"]):
                shutil.copyfile(video_path, clip["clip"])
        finally:
            for path in temporary:
                if os.path.isfile(path):
                    os.remove(path)

        clips = _clip_done(clip["clip"], clip["last"], save_path_tmp)
        if clip["last"] and settings.get()["join"]:
            joined = os.path.splitext(video_path)[0] + "_joined" + os.path.splitext(video_path)[1]
            try:
                if not _concat(clips, joined, video_only=True):
                    raise RuntimeError("the clips could not be joined")
                # The whole soundtrack, added exactly as WanGP does it
                result = original(save_path_tmp, joined, source_audio_tracks, new_audio_tracks, source_audio_duration,
                                  audio_sampling_rate, *args, **kwargs)
                print(f"{LOG_PREFIX} {len(clips)} clips joined into {os.path.basename(save_path_tmp)}")
                _add_final_window(save_path_tmp, clip["clip"], clip["window"])
                return result
            except Exception as error:
                print(f"{LOG_PREFIX} {error}; WanGP saves the whole video instead")
                save_video_original(tensor=clip["frames"], save_file=video_path, **clip["options"])
                return original(save_path_tmp, video_path, source_audio_tracks, new_audio_tracks, source_audio_duration,
                                audio_sampling_rate, *args, **kwargs)
            finally:
                if os.path.isfile(joined):
                    os.remove(joined)
        shutil.copyfile(clip["clip"], save_path_tmp)
        return None

    setattr(combine, _WRAPPED_FLAG, True)
    return combine


def _has_audio(path):
    try:
        from shared.utils.audio_video import _ffprobe_binary
        ffprobe = _ffprobe_binary()
    except Exception:
        ffprobe = "ffprobe"
    result = subprocess.run([ffprobe, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", path],
                            capture_output=True, text=True)
    return bool(result.stdout.strip())


def _join_with_clip_audio(clips, output_path):
    """Join clips: video copied, audio decoded clip by clip and joined (so no gap at the joins) then encoded once."""
    if not _has_audio(clips[0]):
        return _concat(clips, output_path)
    handle, list_path = tempfile.mkstemp(suffix=".txt", prefix="window_clips_")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as listing:
            for clip in clips:
                listing.write("file '" + os.path.abspath(clip).replace("'", "'\\''") + "'\n")
        inputs = ["-f", "concat", "-safe", "0", "-i", list_path]
        for clip in clips:
            inputs += ["-i", clip]
        audio = "".join(f"[{index + 1}:a]" for index in range(len(clips))) + f"concat=n={len(clips)}:v=0:a=1[a]"
        cmd = [_ffmpeg(), "-y", "-v", "error", *inputs, "-filter_complex", audio, "-map", "0:v", "-map", "[a]",
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", output_path]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            print(f"{LOG_PREFIX} joining the clips failed: {result.stderr.strip()[-400:]}")
            return False
        return True
    finally:
        os.remove(list_path)


def _keep_last_window(output, window, gen, configs):
    """Before an interrupted generation's latest output becomes the joined video: keep that last window as its own
    gallery video (a copy, metadata included), listed just before the joined one, like after a normal finish."""
    if not _keeps_windows() or gen is None:
        return None
    try:
        main = sys.modules["__main__"]
        root, extension = os.path.splitext(output)
        window_path = f"{root}_w{window:02d}{extension}"
        shutil.copyfile(output, window_path)
        plain = inspect.unwrap(main.record_file_metadata)
        plain(window_path, configs, False, False, gen, replace_last_file=False, notify_generation=False,
              record_notification=False, write_metadata=False, output_role="intermediate")
        from shared.utils.gallery_sources import VIDEO
        files, file_settings = main.get_processed_queue(gen)[VIDEO.id]  # takes WanGP's lock itself
        with getattr(main, "lock", threading.Lock()):
            if len(files) >= 2 and files[-1] == window_path and files[-2] == output:
                files[-2], files[-1] = files[-1], files[-2]
                file_settings[-2], file_settings[-1] = file_settings[-1], file_settings[-2]
        return window_path
    except Exception as error:
        print(f"{LOG_PREFIX} the last window was not kept on its own: {error!r}")
        return None


def finish_interrupted():
    """After a generation that stopped before its last window (Abort): join the finished windows into its latest output
    file, the one the gallery shows, keeping its WanGP metadata."""
    with _lock:
        run = dict(_run)
        _run.clear()
        _pending.clear()
    clips = [clip for clip in run.get("clips", []) if os.path.isfile(clip)]
    output = run.get("last_output")
    if not settings.get()["enabled"] or not settings.get()["join"] or len(clips) < 2 or not output or not os.path.isfile(output):
        return
    root, extension = os.path.splitext(output)
    joined = root + "_joined" + extension
    try:
        if not _join_with_clip_audio(clips, joined):
            return
        metadata = None
        try:
            from shared.utils.video_metadata import read_metadata_from_video
            metadata = read_metadata_from_video(output)
        except Exception:
            pass
        window_copy = _keep_last_window(output, len(clips), run.get("gen"), metadata)
        os.replace(joined, output)
        if metadata:
            try:
                from shared.utils.video_metadata import save_video_metadata
                save_video_metadata(output, metadata)
            except Exception as error:
                print(f"{LOG_PREFIX} the joined video has no WanGP metadata: {error!r}")
        print(f"{LOG_PREFIX} generation stopped: the {len(clips)} finished windows were joined into {os.path.basename(output)}"
              + (f" (window {len(clips)} on its own: {os.path.basename(window_copy)})" if window_copy else ""))
    except Exception as error:
        print(f"{LOG_PREFIX} could not join the finished windows: {error!r}")
    finally:
        if os.path.isfile(joined):
            os.remove(joined)


def _wrap_generate_media(original):
    @functools.wraps(original)
    def generate_media(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            if _run.get("clips"):  # still open: the generation ended before its last window
                finish_interrupted()

    setattr(generate_media, _WRAPPED_FLAG, True)
    return generate_media


def _wrap_record_file_metadata(original):
    @functools.wraps(original)
    def record_file_metadata(video_path, configs, *args, **kwargs):
        with _lock:
            window_path = _final_windows.pop(os.path.abspath(video_path), None) if isinstance(video_path, str) else None
        if window_path and os.path.isfile(window_path):
            try:
                # WanGP's own function, not the chain: plugins that also watch this call (H3 Latent Carry saves the
                # final video's latents here) see only the joined video, as without this plugin
                plain = inspect.unwrap(original)
                options = {key: value for key, value in kwargs.items() if key == "embedded_images"}
                plain(window_path, configs, *args, replace_last_file=False, notify_generation=False,
                      record_notification=False, output_role="intermediate", **options)
            except Exception as error:
                print(f"{LOG_PREFIX} the last window was not added to the gallery on its own: {error!r}")
        return original(video_path, configs, *args, **kwargs)

    setattr(record_file_metadata, _WRAPPED_FLAG, True)
    return record_file_metadata


def install():
    """Replace WanGP's save_video and audio step with the clip-saving versions. Safe to call more than once."""
    main = sys.modules.get("__main__")
    save_video = getattr(main, "save_video", None)
    combine = getattr(main, "combine_and_concatenate_video_with_audio_tracks", None)
    if not callable(save_video) or not callable(combine) or not hasattr(main, "generate_media"):
        print(f"{LOG_PREFIX} WanGP's video saving functions were not found: clips are not saved")
        return False
    if not getattr(save_video, _WRAPPED_FLAG, False):
        main.save_video = _wrap_save_video(save_video)
        main.combine_and_concatenate_video_with_audio_tracks = _wrap_combine(combine, save_video)
    if not getattr(main.generate_media, _WRAPPED_FLAG, False):
        main.generate_media = _wrap_generate_media(main.generate_media)
    record = getattr(main, "record_file_metadata", None)
    if callable(record) and not getattr(record, _WRAPPED_FLAG, False):
        main.record_file_metadata = _wrap_record_file_metadata(record)
    return True
