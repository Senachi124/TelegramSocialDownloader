# Changelog

## Unreleased

## 1.1.0 — 2026-11-25

- Ship the Takaneko Fanclub feed provider with the bot. Operators can connect their own Fanclub session, manage subscribers in the web console and deliver new posts through Telegram.
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
