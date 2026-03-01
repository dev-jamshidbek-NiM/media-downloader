🎬 Media Downloader (GUI)

A modern desktop media downloader built with Python + CustomTkinter on top of yt-dlp.
Supports YouTube and many other platforms with manual quality selection, download history, pause/resume, and robust anti-blocking handling.

This project focuses on real-world reliability, not just “works on my machine”.

✨ Features
Core

✅ Download Video / Audio

✅ Manual resolution & bitrate selection

✅ Pause / Resume / Retry / Cancel downloads

✅ Per-download progress, speed, ETA

✅ Multiple simultaneous downloads (threaded)

✅ Auto-resume partial downloads

YouTube-Focused Hardening

✅ Handles YouTube SABR streaming

✅ Supports EJS / JS challenge solving

✅ Cookie support (cookies.txt or browser import)

✅ Smart fallback format selection

✅ Prevents re-downloading same video + quality

UI / UX

✅ Clean CustomTkinter UI

✅ Sidebar navigation (Dashboard / Downloader / History / Settings)

✅ Scrollable History cards

✅ Download cards styled consistently across pages

✅ Mouse wheel & touchpad scrolling

✅ Reveal download folder button

✅ Clipboard monitor (optional auto-link capture)

History & Persistence

✅ Download history saved to JSON

✅ De-duplicated history entries

✅ Re-download from history with one click

✅ Resolution buttons generated from fetched formats

🧠 Why this project exists

Most GUI downloaders:

freeze the UI

break on YouTube updates

don’t handle SABR / JS challenges

silently fail on 403 errors

This project is built to:

fail loudly

recover automatically

handle modern YouTube protections

stay usable even when formats change

🛠️ Tech Stack

Python 3.10+

yt-dlp

CustomTkinter

FFmpeg

Node.js (for YouTube JS challenges)

📦 Requirements

Install dependencies:

pip install yt-dlp customtkinter matplotlib


Install FFmpeg and ensure it’s in PATH.

Install Node.js (required for YouTube EJS challenges):

https://nodejs.org/

▶️ Run the App
python main.py

🍪 Cookies (Highly Recommended for YouTube)

Some YouTube videos require login / consent / age verification.

Option 1: cookies.txt

Place a cookies.txt file next to the app root.

Option 2: Browser cookies

The app supports:

cookiesfrombrowser=("chrome",)


This dramatically reduces:

403 Forbidden errors

Missing resolutions

“Only images available” issues

📁 File Naming

Supports templates like:

{title} - {resolution}.{ext}
{uploader}/{title}.{ext}


Automatically appends quality to avoid duplicate downloads.

⚠️ Known Limitations

Some YouTube formats require login or region access

PO Tokens (Android/iOS clients) are not implemented (by design)

DRM-protected streams cannot be downloaded

🚀 Planned Features

⏳ Download queue prioritization

🎧 Separate audio language selection

📺 Playlist manager UI

🌙 True light/dark theme switch

📊 Per-site statistics

🔍 URL auto-classification (site detection)

🧑‍💻 Author

Built by Maverick
Focused on practical tooling, not toy demos.

📜 Disclaimer

This tool is for educational and personal use only.
Respect the terms of service and copyright laws of content platforms.
