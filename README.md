<p align="center">
  <img src="assets/neonquark.svg" alt="NeonQuark — personal scripts, small by design" width="100%">
</p>

<p align="center">
  <strong>A little collection of scripts I use personally.</strong><br>
  Small tools, quiet improvements, fewer repetitive steps.
</p>

---

## In the collection

| Script | What it does | Get it |
| :--- | :--- | :--- |
| **Ion Drift** | A lightweight browser navigation helper for my everyday setup. | [Install userscript](https://raw.githubusercontent.com/XBadFellaX/NeonQuark/main/ion-drift.user.js) |

## Add to AdGuard

For the full **AdGuard for Android** app:

1. Open **Settings → Filtering → Extensions**.
2. Choose **Add extension → Import from file or URL**.
3. Paste the [Ion Drift script URL](https://raw.githubusercontent.com/XBadFellaX/NeonQuark/main/ion-drift.user.js) and enable it.
4. Keep protection and HTTPS filtering enabled for your browser. Finish AdGuard's certificate setup if prompted.

You can also download the `.user.js` file and import it from your phone's storage. AdGuard DNS alone cannot run userscripts; AdGuard for iOS does not provide this custom userscript manager.

## Small by design

- **Self-contained.** No dependencies, remote libraries, or build step.
- **Limited scope.** Runs only on the pages listed in the script's metadata.
- **Flexible addresses.** Follows single-part domain-extension changes automatically, including `www` variants, while ignoring unrelated hostnames. A changed site name or compound suffix requires an update.
- **No added telemetry.** No analytics or external collection service.
- **Easy to keep current.** Update URLs are included for managers that support update checks.

## Local checks

With Node.js installed:

```sh
node --check ion-drift.user.js
node --test test-ion-drift.cjs
```

The tests cover decoding, domain-extension changes, hostname isolation, late-loading content, stale stored state, destination validation, and cleanup.

## Personal toolkit

These scripts are written around my own setup and shared as-is. Page changes can require an update. Review the source before installing; disable a script in your manager whenever you want to stop it running.

<p align="center"><sub>NeonQuark · Personal scripts. Small by design.</sub></p>
