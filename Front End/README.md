# ResQ hotspot dashboard

Copy **index.html** and **alerts.json** into the existing `~/resq` folder on the Arduino UNO Q. Keep both filenames lowercase and side by side. The dashboard uses your existing Python server and needs no installation, API key, or build.

## Develop on a computer

From the folder containing `index.html` and `alerts.json`, run `python -m http.server 8080 --bind 127.0.0.1` and open [http://localhost:8080](http://localhost:8080). Use `python3` if that is the command installed on your computer.

- `index.html` contains the markup, styles, and browser logic. The script handles location selection, Open-Meteo requests, browser storage, demo mode, and rendering local notices.
- `alerts.json` is the static local notice feed. See the notice format below before replacing it with an app integration.
- When changing the app, test both localhost and the Arduino's HTTP hotspot address. Localhost can support geolocation with permission even when GPS is blocked on the hotspot address. Cached readings and saved locations are separate for each address.

## Replace the dashboard

1. Open the same Arduino terminal or file-transfer tool you used for the original dashboard. In the Arduino terminal, back up your old page:

   ```sh
   cd ~/resq
   cp -i index.html index.backup.html
   ```

2. Transfer the two downloaded files into `~/resq`. If you already use SSH file transfer, connect the computer to the ResQ hotspot, open PowerShell in the download folder, and run the following. Use the Arduino username from your existing setup if it differs from `arduino`. If you already have an `alerts.json` file, back it up before copying the new one.

   ```powershell
   scp index.html alerts.json arduino@10.42.0.1:resq/
   ```

3. If the existing server is running, leave it running. If it is stopped and you are starting it by hand, run this on the Arduino and leave the terminal open:

   ```sh
   cd ~/resq
   python3 -m http.server 8080 --bind 0.0.0.0
   ```

4. On your phone, join the ResQ Wi-Fi hotspot and stay connected if the phone warns that Wi-Fi has no internet. Open [http://10.42.0.1:8080](http://10.42.0.1:8080) and reload if you see the old page. If you previously enabled automatic startup for the same folder, the new files will be served by that setup. If the page fails to open, check that the phone stayed on the hotspot and that the existing server is running. An “address already in use” error usually means something is already listening on port 8080.

## Try the dashboard

- **Time.** The banner uses the phone or computer’s own clock and time zone. It works without internet. Changing the weather location leaves that clock tied to the browsing device.
- **Location.** Start with the South Florida preset (Miami area) or enter decimal latitude and longitude for your area. The preset is opt-in. “Use my location” requests browser permission only when the page runs in a supported secure context. The Arduino’s plain HTTP LAN address generally blocks GPS. The localhost exception only applies on the same device as the browser. Permission denial, blocked access and timeouts leave the preset and manual entry available.
- **Current weather.** Choose a location and tap **Fetch weather**. The browser sends the selected coordinates to Open-Meteo. The phone needs an internet route while it can still load the hotspot page. Cellular data may provide that route on some phones. Open-Meteo reports model-based conditions for the selected location. The free endpoint is intended for non-commercial use. See [Open-Meteo API documentation](https://open-meteo.com/en/docs) and [terms](https://open-meteo.com/en/terms).
- **Offline test.** After one successful fetch, keep the phone connected to the hotspot, disable its internet route and reload the same address. The dashboard should show **Cached · Last saved weather**, or **Stale** when either the data time or retrieval time reaches one hour. It also shows both timestamps. Fetching again should retain the reading if the request fails. With no prior successful fetch, expect **Weather unavailable**. The cache belongs to this browser and this exact address and port. Private browsing, clearing site data or changing addresses can remove access to it. Selecting different coordinates clears the previous location’s reading.
- **Demo.** Tap **Show demo** to see clearly labeled fictional weather without internet. Exit demo to return to real, saved or unavailable weather. Sample values are never saved as weather.
- **Reset.** **Clear saved data** removes the selected location and weather snapshot from this browser.

## Post a local emergency notice

The included `alerts.json` starts empty. An operator can replace the file on the Arduino with a JSON array of notices. The page reads it on load and every 30 seconds while open. Use a time with an explicit zone and set the correct clock on the viewing device, which decides whether the notice has expired. If possible, prepare the file under another name and rename it to `alerts.json` after editing so visitors do not see a partially written file.

Example for an operator drill. Adjust the date to a future UTC time before testing, then restore `[]` when the drill ends.

```json
[
  {
    "title": "DRILL ONLY — ResQ notice test",
    "message": "This is a practice message for the ResQ hotspot.",
    "expiresAt": "2027-01-01T18:00:00Z"
  }
]
```

Missing or invalid files show **Alert status is unknown**. A valid file with no unexpired notices shows **No active operator notices are posted**. The dashboard only displays operator-posted notices and does not monitor official warnings or send background notifications. Use your local emergency authorities for official warnings.

## Restore the previous page

Run `cd ~/resq` and `cp -i index.backup.html index.html` on the Arduino, then reload the page on your phone.

