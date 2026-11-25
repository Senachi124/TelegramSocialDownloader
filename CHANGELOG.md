# Changelog

## Unreleased

- Connect or switch Fanclub accounts with a single refresh token in **Takaneko subscriptions**. Use **Test connection** to verify the saved login, including while the provider is paused.

- Add a read-only Error log for failed, partial or paused downloads, Telegram push errors and provider health reports. Takaneko subscriptions now contains Fanclub login and recipient settings. Takaneko errors are logged without sending owner reminders.
- Rename the subscription management page to **Takaneko subscriptions**.
- Redesign the web console for desktop and mobile. Connection settings save independently, access and subscription rows show available actions, and credential edits return to the selected pool.
- Allow operators to test a development build on a selected installation by manually deploying its verified CI artifact and commit SHA.

- Add a Features page to the web console for individual download platforms, Telegram translation and OCR, and the optional Takaneko Fanclub service. Paused download platforms and Fanclub subscriptions retain pending work. New Fanclub installations start paused.
- Fix browser sign-in and settings updates when the browser applies the console’s referrer policy to form submissions.
- Organize platform adapters, worker processing, translation and web administration into focused modules. Existing service commands and platform import paths remain available.

## 1.1.1 — 2026-11-25

- Verify the selected media volume and provider services during setup and release upgrades.
- Continue dispatching to eligible subscribers when other recipients lose access or cannot be checked.
- Recover a completed Fanclub attachment after an interrupted receipt write and preserve a differing local file for review.

## 1.1.0 — 2026-11-25

- Include a dedicated Fanclub credential broker in the release wheel. Database checkpoints use the configured service identity and groups.

- Ship the Takaneko Fanclub feed provider with the bot. Operators can connect their own Fanclub session, manage subscribers in the web console and deliver new posts through Telegram.
- Register optional services before activation so an interrupted installation can resume. Test their control socket as the Bot service account on a fresh Linux installation.
- Keep Fanclub discovery, subscription state and delivery checkpoints in a separate service package. The Linux deployment guide covers installing and updating both services.

## 1.0.3 — 2026-11-25

- Remove Telegram deployment announcements and retire outstanding announcement approvals.

- Select the latest published release in the installation guide.

## 1.0.2 — 2026-11-25

- Return the production deployment receipt through the public status endpoint so the Actions job can confirm activation.

## 1.0.1 — 2026-11-25

- Install the standalone Linux service and deploy tested releases from the production workflow.

## 1.0.0 — 2026-11-25

- Download social media from Telegram, select delivery preferences and receive direct files or expiring links.
- Manage access, subscriptions, failure alerts, API credentials and platform account pools from the web console.
- Build and publish versioned Python distributions with GitHub Actions.
