DedSec CCTV - your own stream catalog
=====================================

This folder is yours. The app reads it and shows it in Add CCTV under "My streams".
It is never overwritten (missing country folders are re-created, nothing else is touched).

Layout
  user_streams/<Country>/streams.json     list of your streams for that country
  user_streams/<Country>/banners/         optional preview images (PNG/JPG/WebP)

streams.json format (a JSON list, or {"streams": [...]}):
  [
    {
      "id": "uk-home-1",                      unique id (letters, digits, - _ .)
      "name": "Front door",                   shown on the card
      "city": "London",
      "rtsp": "rtsp://user:pass@192.0.2.10:554/stream1",
                                              rtsp://, http(s):// MJPEG, or .m3u8 HLS URL
      "banner": "banners/front-door.jpg",     optional, relative to this country folder
      "category": "residential"               "residential" or "traffic"
    }
  ]

Entries with "example": true are templates and are ignored by the app.
Copy one, remove the "example" line, and fill in your own camera.
The example addresses (192.0.2.x, example.com) are documentation-only and do not exist.
Reopen Add CCTV after editing.
