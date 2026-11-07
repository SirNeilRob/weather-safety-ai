# ResQ hotspot dashboard

Copy **index.html** and **alerts.json** into the existing `~/resq` folder on the Arduino UNO Q. Keep both filenames lowercase and side by side. The dashboard uses your existing Python server and needs no installation, API key, or build.

## Develop on a computer

From the folder containing `index.html` and `alerts.json`, run `python -m http.server 8080 --bind 127.0.0.1` and open [http://localhost:8080](http://localhost:8080). Use `python3` if that is the command installed on your computer.

- `index.html` contains the markup, styles, and browser logic. Everything it shows comes from the ResQ backend (`/v1/health`, `/v1/noaa`, `/v1/alerts/active`, `/v1/queue/summary`, `/v1/jobs`), plus api.weather.gov for **Find live alert** and the local `alerts.json` notices. When the page is served on :8080 it calls the backend on :8000 of the same host.
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

- **Statuses.** The top bar shows three separate statuses from the backend: **Internet** (the board can reach api.weather.gov), **Swarms** (last Swarms call ok/failed and its time in ms) and **NOAA** (last poll ok/failed, countdown to the next one).
- **Location.** There is no default location. Until one is set, the NOAA panel shows **Set location** and no weather. Set it with **Use my location** (browser GPS, only in a secure context such as localhost; the Arduino's plain HTTP address generally blocks it), by entering coordinates, by tapping **Find live alert**, or with `NOAA_LAT`/`NOAA_LON` on the backend.
- **Find live alert.** Fetches active alerts from api.weather.gov in the browser (the viewing device needs internet), picks one, moves the backend's NOAA point inside its area and polls. The card shows the NWS alert ID and links to it on api.weather.gov.
- **Who wrote it.** Each card says whether Swarms AI or local rules wrote it, per pipeline stage.
- **Offline.** The last backend answers are saved in this browser and shown, with their time, when the backend cannot be reached. Missing values are shown as "—".

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

