LOOT FARMER - SETUP GUIDE
=========================

You need: a Windows 10/11 PC or laptop, internet, and about 20 minutes.
The game must be in ENGLISH (the bot recognises the English buttons).


STEP 1 - Unzip
--------------
Right-click LootFarmer_share.zip > "Extract All..." and put the folder somewhere
you'll keep it (e.g. Documents). Don't run anything from inside the zip.


STEP 2 - Install BlueStacks 5 and Clash of Clans
------------------------------------------------
1. Download BlueStacks 5 from https://www.bluestacks.com and install it.
2. Open BlueStacks, sign in to Google Play, install Clash of Clans.
3. Open Clash of Clans and log in to your account(s) with Supercell ID.
   (For account rotation, every account you want farmed should appear in
   Settings (cog) > blue switch-account button.)
4. CLOSE BlueStacks.


STEP 3 - Run the setup
----------------------
Double-click  Setup.bat  in the folder.
  - If Windows says "Windows protected your PC": click "More info" > "Run anyway".
  - If Windows asks for permission to install something: click "Yes".
  - Answer its questions with y (yes) or n (no).

It installs everything the bot needs and sets BlueStacks up for it:
  - Python + the bot's packages
  (Tesseract OCR and ADB / platform-tools are already included in the folder - nothing to install)
  - cloudflared (the phone link that works away from home)
  - BlueStacks: turns ON Android Debug Bridge (ADB), sets resolution to
    1920 x 1080 with DPI 240, and sets the phone profile to
    Samsung Galaxy S22 Ultra
  - On laptops with two graphics chips it asks whether to run BlueStacks
    on the Intel chip (say yes if BlueStacks ever crashes on the loading clouds)
  - Fills in the bot's settings and puts "Loot Farmer" on your desktop

Running it again is safe - it skips anything already done. If a step shows
a red "!!" message, do what it says (or send a screenshot of the window).


STEP 4 - Check BlueStacks (only if something doesn't work)
-----------------------------------------------------------
Setup does these for you. To check or set them by hand, open BlueStacks >
Settings (gear icon, bottom-right of the side bar):
  - Display:   Resolution 1920 x 1080,  Pixel density 240 DPI
  - Phone:     Device profile  Samsung Galaxy S22 Ultra
  - Advanced:  Android Debug Bridge (ADB)  ON
Click "Save changes" and let BlueStacks restart.


STEP 5 - Start farming
----------------------
1. Open BlueStacks and Clash of Clans, go to your home village.
2. Open "Loot Farmer" from the desktop.
3. The top right should say "Connected". If it says "Offline", click the
   refresh (circle arrow) button next to it.
4. Setup tab > "Run setup check" - the log should say everything is set up.
5. Settings tab - choose what you want, then "Save settings":
     Loot:      "Attack every base" (fast) or set minimum gold/elixir
     Upgrades:  Builders / Lab / Walls on or off
     Accounts:  "Rotate accounts" to move to your next account when all
                builders and the lab are busy; "Stop the bot when every
                account is busy" to stop once there's nothing left to do
     Battle:    auto-deploy and hero abilities are on already
6. Press "Start farming".


PHONE VIEW
----------
Click "Anywhere link" under the title to copy it, then open it on your phone.
It works from anywhere (Wi-Fi or mobile data), is posted to Discord when the
app starts, and stays the same until the PC restarts. Windows may ask to
let Python use the network the first time - click "Allow".


OPTIONAL - AI helper
--------------------
A free Groq API key (console.groq.com) pasted into Settings > Engine lets
the bot ask an AI what to do on unexpected screens/popups. Everything works
without it.
