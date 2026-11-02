# Media Asset Generation & Optimization Guide: YouTube, GIF, WebP & Video Transcoding

This guide documents the exact workflows, scripts, and commands used to generate, trim, and optimize web media assets (animated GIFs, animated WebP, and streaming WebM/MP4 video clips) for low-latency web applications.

You can copy and reuse these scripts and patterns in any repository or project.

---

## Table of Contents
1. [Core Strategy & Performance Architecture](#1-core-strategy--performance-architecture)
2. [Prerequisites & Toolchain Installation](#2-prerequisites--toolchain-installation)
3. [YouTube to GIF Extraction (`youtube-to-gif.py`)](#3-youtube-to-gif-extraction-youtube-to-gifpy)
4. [Local Video to GIF Conversion (`ffmpeg` Command Line)](#4-local-video-to-gif-conversion-ffmpeg-command-line)
5. [Transcoding Animated GIFs to Animated WebP (`sharp` & CLI)](#5-transcoding-animated-gifs-to-animated-webp-sharp--cli)
6. [Converting Video to WebM and FastStart MP4 for HTML5 `<video>`](#6-converting-video-to-webm-and-faststart-mp4-for-html5-video)
7. [Frontend React Component Implementations](#7-frontend-react-component-implementations)
8. [Markdown `#no-embed` YouTube Deduplication Pattern](#8-markdown-no-embed-youtube-deduplication-pattern)
9. [Complete Standalone Scripts (Ready to Copy)](#9-complete-standalone-scripts-ready-to-copy)

---

## 1. Core Strategy & Performance Architecture

### Why Avoid Raw Animated GIFs?
- **Massive Payload:** Raw GIFs easily exceed 15MB–30MB per 5-second clip because GIF lacks inter-frame motion compression.
- **Color Degradation:** Limited to a single 256-color indexed palette per frame, causing banding and graininess.
- **Main-Thread CPU Overhead:** Decoding large animated GIFs on mobile browsers spikes CPU and memory usage, degrading Core Web Vitals (LCP/INP).

### The Replacement Strategy
| Media Type | Recommended Format | Tooling | Disk / Bandwidth Savings |
| :--- | :--- | :--- | :--- |
| **Short UI / Robot Animations (<10s)** | **Animated WebP (`.webp`)** | `sharp` (Node.js) or `gif2webp` | **70% – 85% smaller** than GIF |
| **Longer Demos / High FPS (>10s)** | **Dual-Source Video (`.webm` + `.mp4`)** | `ffmpeg` (VP9 + H.264 FastStart) | **90% – 95% smaller** than GIF |
| **External Video Links** | **Clickable WebP + `#no-embed` YouTube Link** | Custom markdown renderer | Eliminates loading heavy YouTube iframes |

---

## 2. Prerequisites & Toolchain Installation

Ensure the following tools are installed on your machine or CI runner:

### Linux / Ubuntu
```bash
# ffmpeg and libwebp tools
sudo apt update
sudo apt install -y ffmpeg webp

# Python packages
pip install yt-dlp

# Node.js sharp (for automated project asset pipelines)
npm install --save-dev sharp
# or: pnpm add -D sharp
```

### macOS (Homebrew)
```bash
brew install ffmpeg webp yt-dlp
pnpm add -D sharp
```

---

## 3. YouTube to GIF Extraction (`youtube-to-gif.py`)

Instead of downloading multi-gigabyte YouTube videos to your disk, use `yt-dlp` to extract the **direct streaming URL** of the video stream and pipe it straight into `ffmpeg` with a high-quality two-pass palette filter.

### Standalone Python Command
```bash
python3 scripts/youtube-to-gif.py "https://www.youtube.com/watch?v=2mGN3ka_7i0" \
  output.gif \
  --start 00:00:15 \
  --duration 6 \
  --fps 12 \
  --width 640 \
  --speed 1.5
```

### Key Parameters:
- `--start`: Start timestamp (`HH:MM:SS` or seconds like `45`).
- `--duration`: Duration of the clip in seconds (keep under 5–8s for web clips).
- `--fps`: Target framerate (`10`–`15` fps is optimal for web animations; 12 fps provides smooth motion with small file sizes).
- `--width`: Output pixel width (`640` or `480` scales proportionally while preserving aspect ratio).
- `--speed`: Playback speedup factor (e.g. `1.5` or `2.0` speeds up slow physical demonstrations and halves the frame count).

### How the `ffmpeg` Two-Pass Palette Filter Works:
Standard `ffmpeg -i video.mp4 output.gif` produces terrible color banding. The secret to high-quality GIFs is **palette generation**:
```text
[0:v] fps=12,scale=640:-1:flags=lanczos,split[s0][s1];
[s0] palettegen=max_colors=256[p];
[s1][p] paletteuse=dither=sierra2_4a
```
1. `split[s0][s1]`: Duplicates the video stream into two branches.
2. `palettegen=max_colors=256[p]`: Scans the clip frames and builds an optimized custom 256-color palette.
3. `paletteuse=dither=sierra2_4a`: Applies the generated palette to the frames using Sierra-2-4A error diffusion dithering, eliminating color bands.

---

## 4. Local Video to GIF Conversion (`ffmpeg` Command Line)

If you have a local video file (`.mp4`, `.mov`, `.mkv`), you can generate an optimized GIF directly with this one-liner:

```bash
ffmpeg -y -ss 00:00:05 -t 6 -i input.mp4 \
  -filter_complex "[0:v]fps=12,scale=640:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=256[p];[s1][p]paletteuse=dither=sierra2_4a" \
  output.gif
```

### Adding Playback Speedup (e.g., 2x Speed):
```bash
ffmpeg -y -ss 00:00:10 -t 10 -i input.mp4 \
  -filter_complex "[0:v]setpts=0.5*PTS,fps=12,scale=640:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=256[p];[s1][p]paletteuse=dither=sierra2_4a" \
  output_2x.gif
```

---

## 5. Transcoding Animated GIFs to Animated WebP (`sharp` & CLI)

Converting raw animated GIFs to animated WebP yields dramatic size reductions (typically **70% to 85% smaller**) while preserving 100% of the framerate and visual fidelity.

### Method A: Automated Batch Script with `sharp` (Recommended for Projects)
In Node.js, `sharp` with `{ animated: true }` processes multi-frame animated GIFs into WebP:

```javascript
import sharp from 'sharp';

await sharp('input.gif', { animated: true })
  .webp({
    quality: 80,      // 75-80 is visually indistinguishable from source
    effort: 4,       // Compression effort 1-6 (4 is optimal speed/ratio balance)
    lossless: false  // Lossy WebP yields 80%+ savings
  })
  .toFile('output.webp');
```

Run this across all assets automatically:
```bash
pnpm run build:media
# or: node scripts/optimize-media.js --delete-gifs
```

### Method B: Pure CLI with `gif2webp`
```bash
gif2webp -q 80 -m 4 input.gif -o output.webp
```

### Method C: Pure CLI with `ffmpeg`
```bash
ffmpeg -i input.gif -vcodec libwebp -lossless 0 -q:v 80 -loop 0 output.webp
```

### Real-World Compression Results from this Portfolio:
| File | Original GIF Size | WebP Size | Reduction |
| :--- | :--- | :--- | :--- |
| `pr2_grasp_1` | 21.5 MB | **6.0 MB** | **72%** |
| `delivery_coordination` | 15.9 MB | **3.9 MB** | **75%** |
| `duckietown_navigation` | 12.3 MB | **1.7 MB** | **86%** |
| `icra_presentation` | 3.1 MB | **611 KB** | **80%** |

---

## 6. Converting Video to WebM and FastStart MP4 for HTML5 `<video>`

For clips longer than 8–10 seconds, continuous video loops via HTML5 `<video>` are even lighter than WebP and utilize GPU hardware decoding.

Always provide both **WebM (VP9)** and **MP4 (H.264 FastStart)** sources for maximum cross-browser compatibility:

### 1. Generating WebM (VP9 Codec)
```bash
ffmpeg -i input.mov \
  -c:v libvpx-vp9 \
  -b:v 0 \
  -crf 32 \
  -an \
  output.webm
```
- `-c:v libvpx-vp9`: State-of-the-art VP9 open video compression.
- `-b:v 0 -crf 32`: Constant quality factor (CRF 30–35 is ideal for web clips).
- `-an`: Strips audio track to eliminate unnecessary bytes for muted background loops.

### 2. Generating MP4 (H.264 with FastStart)
```bash
ffmpeg -i input.mov \
  -c:v libx264 \
  -pix_fmt yuv420p \
  -preset slow \
  -crf 23 \
  -movflags +faststart \
  -an \
  output.mp4
```
- `-pix_fmt yuv420p`: Essential for compatibility with Apple Safari and iOS devices.
- `-movflags +faststart`: Moves the `moov atom` (video index metadata) to the beginning of the MP4 file so the browser begins playing immediately without downloading the whole file first.
- `-an`: Strips audio track.

### 3. Generating a Poster Fallback Frame
```bash
ffmpeg -ss 00:00:01 -i input.mp4 -vframes 1 -q:v 2 poster.webp
```

---

## 7. Frontend React Component Implementations

### A. `SafeImage` Component (`<picture>` Wrapper)
Wraps images in modern `<picture>` tags with WebP `<source>` elements and fallback handling:

```tsx
import React, { useState } from 'react';

interface SafeImageProps extends React.ImgHTMLAttributes<HTMLImageElement> {
  webpSrc?: string;
  fallbackSrc?: string;
  containerClassName?: string;
}

export const SafeImage: React.FC<SafeImageProps> = ({
  src,
  alt,
  webpSrc,
  fallbackSrc,
  containerClassName = '',
  className = '',
  ...props
}) => {
  const [hasError, setHasError] = useState(false);
  const [isLoading, setIsLoading] = useState(true);

  // Auto-infer .webp variant if not explicitly provided
  const inferredWebp = webpSrc || src?.replace(/\.(png|jpe?g|gif)$/i, '.webp');

  if (hasError && fallbackSrc) {
    return <img src={fallbackSrc} alt={alt} className={className} {...props} />;
  }

  return (
    <div className={`relative overflow-hidden ${containerClassName}`}>
      {isLoading && <div className="absolute inset-0 animate-pulse bg-gray-200" />}
      <picture>
        {inferredWebp && <source srcSet={inferredWebp} type="image/webp" />}
        <img
          src={src}
          alt={alt}
          loading="lazy"
          decoding="async"
          className={`transition-opacity duration-300 ${isLoading ? 'opacity-0' : 'opacity-100'} ${className}`}
          onLoad={() => setIsLoading(false)}
          onError={() => {
            setHasError(true);
            setIsLoading(false);
          }}
          {...props}
        />
      </picture>
    </div>
  );
};
```

### B. `OptimizedVideo` Component (Looping Dual-Source Video)
Renders a silent looping background demonstration with loading pulse skeletons and accessibility fallbacks:

```tsx
import React, { useState } from 'react';

interface OptimizedVideoProps extends React.VideoHTMLAttributes<HTMLVideoElement> {
  webmSrc?: string;
  mp4Src?: string;
  poster?: string;
  ariaLabel?: string;
}

export const OptimizedVideo: React.FC<OptimizedVideoProps> = ({
  webmSrc,
  mp4Src,
  poster,
  ariaLabel,
  className = '',
  ...props
}) => {
  const [isLoading, setIsLoading] = useState(true);
  const [hasError, setHasError] = useState(false);

  return (
    <div className="relative overflow-hidden bg-black/5">
      {isLoading && <div className="absolute inset-0 animate-pulse bg-gray-300" />}
      {!hasError ? (
        <video
          autoPlay
          loop
          muted
          playsInline
          poster={poster}
          aria-label={ariaLabel}
          className={`w-full h-full object-cover transition-opacity duration-300 ${
            isLoading ? 'opacity-0' : 'opacity-100'
          } ${className}`}
          onLoadedData={() => setIsLoading(false)}
          onError={() => {
            setHasError(true);
            setIsLoading(false);
          }}
          {...props}
        >
          {webmSrc && <source src={webmSrc} type="video/webm" />}
          {mp4Src && <source src={mp4Src} type="video/mp4" />}
          Your browser does not support HTML5 video.
        </video>
      ) : (
        <div className="p-4 text-center text-sm text-gray-500">Video playback unavailable</div>
      )}
    </div>
  );
};
```

---

## 8. Markdown `#no-embed` YouTube Deduplication Pattern

When rendering markdown research articles or case studies, having both an animated preview GIF/WebP **and** an embedded YouTube `<iframe>` on the same page hurts performance and duplicates content.

### Markdown Syntax
Wrap the WebP animation in a link to the YouTube video with `#no-embed` appended:

```markdown
[![Robot whole-arm grasp demonstration](/assets/research/my_demo.webp#max-w-2xl)](https://www.youtube.com/watch?v=2mGN3ka_7i0#no-embed)
```

### Custom React Markdown Link Renderer
In your markdown parser (e.g. `react-markdown`), intercept links:

```tsx
const components = {
  a: ({ href, children, ...props }) => {
    if (href) {
      const isNoEmbed = href.includes('no-embed');
      const cleanHref = href.replace(/[?#]no-embed/, '');

      // If YouTube link does NOT have #no-embed, render an iframe player
      if ((cleanHref.includes('youtube.com') || cleanHref.includes('youtu.be')) && !isNoEmbed) {
        const videoId = cleanHref.match(/(?:watch\?v=|embed\/|youtu\.be\/)([a-zA-Z0-9_-]{11})/)?.[1];
        return (
          <div className="aspect-video w-full">
            <iframe
              src={`https://www.youtube.com/embed/${videoId}`}
              title="YouTube video"
              className="w-full h-full border-0"
              allowFullScreen
            />
          </div>
        );
      }

      // If #no-embed is present, render standard clickable anchor to YouTube
      return (
        <a href={cleanHref} target="_blank" rel="noopener noreferrer" {...props}>
          {children}
        </a>
      );
    }
    return <a href={href} {...props}>{children}</a>;
  }
};
```

---

## 9. Complete Standalone Scripts (Ready to Copy)

### Script 1: `youtube-to-gif.py`
Save this file into `scripts/youtube-to-gif.py`:

```python
#!/usr/bin/env python3
"""
youtube-to-gif.py
Extracts a segment from a YouTube video or direct URL and outputs a high-quality,
palette-optimized animated GIF without downloading full-length video files.
"""

import os
import sys
import subprocess
import argparse

def install_dependencies():
    try:
        import yt_dlp
    except ImportError:
        print("Installing yt-dlp...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "yt-dlp"])

def convert_youtube_to_gif(url, start_time, duration, output_path, fps=12, width=640, speed=1.0):
    install_dependencies()
    import yt_dlp

    print(f"Fetching video stream URL from: {url}...")
    ydl_opts = {
        'format': '18/22/135/134/best[ext=mp4]/best',
        'quiet': True,
        'no_warnings': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            video_url = info.get('url')
            title = info.get('title', 'Video')
            print(f"Found video stream for: '{title}'")
        except Exception as e:
            print(f"Error fetching YouTube stream: {e}", file=sys.stderr)
            sys.exit(1)

    if not video_url:
        print("Could not retrieve a direct stream URL.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    source_duration = duration * speed
    print(f"Processing segment from {start_time} (duration: {source_duration}s at {speed}x speed)...")

    filter_complex = f"[0:v]fps={fps},scale={width}:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=256[p];[s1][p]paletteuse=dither=sierra2_4a"
    if speed != 1.0:
        filter_complex = f"[0:v]setpts={1.0/speed}*PTS,fps={fps},scale={width}:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=256[p];[s1][p]paletteuse=dither=sierra2_4a"

    ffmpeg_cmd = [
        "ffmpeg",
        "-y",
        "-ss", start_time,
        "-t", str(source_duration),
        "-i", video_url,
        "-filter_complex", filter_complex,
        output_path
    ]

    try:
        subprocess.run(ffmpeg_cmd, check=True)
        print(f"Success! Output GIF saved to: {output_path}")
    except subprocess.CalledProcessError as e:
        print(f"FFmpeg execution failed: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert YouTube segment to high-quality GIF.")
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument("output", help="Output GIF path (e.g. output.gif)")
    parser.add_argument("--start", default="00:00:00", help="Start time (HH:MM:SS or SS)")
    parser.add_argument("--duration", type=int, default=5, help="Duration in seconds (default: 5)")
    parser.add_argument("--fps", type=int, default=12, help="Framerate (default: 12)")
    parser.add_argument("--width", type=int, default=640, help="Output width (default: 640)")
    parser.add_argument("--speed", type=float, default=1.0, help="Speedup factor (e.g. 1.5, 2.0)")

    args = parser.parse_args()
    convert_youtube_to_gif(args.url, args.start, args.duration, args.output, args.fps, args.width, args.speed)
```

---

### Script 2: `optimize-media.js`
Save this file into `scripts/optimize-media.js`:

```javascript
#!/usr/bin/env node
/**
 * optimize-media.js
 * Scans image directories and automatically converts PNG, JPG, and animated GIF
 * files to modern WebP variants using sharp.
 */

import fs from 'fs';
import path from 'path';
import sharp from 'sharp';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootDir = path.resolve(__dirname, '..');

const TARGET_DIRS = [
  path.join(rootDir, 'public/assets'),
  path.join(rootDir, 'public/images'),
  path.join(rootDir, 'src/assets'),
];

export function getImagesInDirectory(dirPath, fileList = []) {
  if (!fs.existsSync(dirPath)) return fileList;
  const entries = fs.readdirSync(dirPath, { withFileTypes: true });

  for (const entry of entries) {
    const fullPath = path.join(dirPath, entry.name);
    if (entry.isDirectory()) {
      getImagesInDirectory(fullPath, fileList);
    } else if (/\.(png|jpe?g|gif)$/i.test(entry.name)) {
      fileList.push(fullPath);
    }
  }
  return fileList;
}

export async function processImage(imagePath, options = {}) {
  const parsed = path.parse(imagePath);
  const webpPath = path.join(parsed.dir, `${parsed.name}.webp`);
  const isGif = /\.gif$/i.test(imagePath);

  // Skip if WebP exists and is newer than source
  if (fs.existsSync(webpPath)) {
    const origStat = fs.statSync(imagePath);
    const webpStat = fs.statSync(webpPath);
    if (webpStat.mtimeMs >= origStat.mtimeMs) {
      return { status: 'skipped', path: webpPath };
    }
  }

  try {
    const pipeline = sharp(imagePath, isGif ? { animated: true } : {});
    await pipeline
      .webp({ quality: 80, effort: 4, lossless: false })
      .toFile(webpPath);

    if (options.deleteOriginal && isGif && fs.existsSync(webpPath)) {
      fs.unlinkSync(imagePath);
      return { status: 'created_and_deleted', path: webpPath, originalDeleted: imagePath };
    }

    return { status: 'created', path: webpPath };
  } catch (err) {
    console.error(`Failed to optimize: ${imagePath}`, err);
    return { status: 'failed', error: err };
  }
}

export async function main() {
  console.log('Starting media asset optimization...');
  const shouldDeleteOriginalGifs = process.argv.includes('--delete-gifs');
  let totalProcessed = 0, totalCreated = 0, totalSkipped = 0;

  for (const dir of TARGET_DIRS) {
    const images = getImagesInDirectory(dir);
    for (const img of images) {
      totalProcessed++;
      const result = await processImage(img, { deleteOriginal: shouldDeleteOriginalGifs });
      if (result.status === 'created' || result.status === 'created_and_deleted') totalCreated++;
      if (result.status === 'skipped') totalSkipped++;
    }
  }

  console.log(`Optimization complete: ${totalProcessed} checked, ${totalCreated} WebP generated, ${totalSkipped} skipped.`);
}

const isDirectRun = process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1]);
if (isDirectRun) {
  main().catch((err) => {
    console.error('Error optimizing media:', err);
    process.exit(1);
  });
}
```
