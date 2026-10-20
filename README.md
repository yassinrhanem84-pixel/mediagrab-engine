# MediaGrab
Double-click `run.bat` (needs Python 3 + ffmpeg + Node.js on PATH), or: `pip install -r requirements.txt && python app.py` -> http://localhost:5000

- Paste any link -> "Get video & qualities" lists every quality the site offers (144p up to 8K, MP4) plus MP3/audio.
- "Get profile picture" works with profile links (youtube.com/@x, tiktok.com/@x, x.com/x, instagram.com/x, facebook.com/x) or with a video/post link (it finds the uploader).
- Keep yt-dlp fresh (sites change often): `pip install -U yt-dlp` (run.bat does this every start).
- YouTube / Instagram / Facebook sometimes demand a login or bot check. Fix: export cookies with the
  "Get cookies.txt LOCALLY" browser extension, save as `cookies.txt` next to app.py. It is picked up automatically
  (also used for Instagram profile pictures, which are login-walled).
