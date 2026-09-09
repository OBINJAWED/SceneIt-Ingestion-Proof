# Synthetic journey video

`synthetic-journey-h264.mp4` is generated locally and contains only FFmpeg test
patterns and a generated sine tone. It is six seconds long, 320x180, H.264
Constrained Baseline/yuv420p with AAC audio and a fast-start MP4 layout.

Recreate it from this directory with:

```sh
ffmpeg -f lavfi -i "testsrc2=size=320x180:rate=24:duration=6" \
  -f lavfi -i "sine=frequency=660:sample_rate=48000:duration=6" \
  -c:v libx264 -profile:v baseline -pix_fmt yuv420p -preset veryslow -crf 32 \
  -movflags +faststart -c:a aac -b:a 48k -shortest -y synthetic-journey-h264.mp4
```

No external media or provider is used.