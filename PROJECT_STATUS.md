# Tally Stock Visibility System - Project Status

## Status

Operational. The app supports admin/customer login roles, searchable Select2-driven workflows, stable stock-update refresh behavior, account lifecycle management (create/delete/pause/resume), and a Tally fallback path that keeps the last saved export usable when live refresh fails.

## Completed

- Role-based login with `admin` / `customer` modes.
- Admin credentials fixed to username `admin` and password `idris123`.
- Customer mode is read-only and can browse car stock, images, and dropdowns.
- Admin-only actions are restricted for reload, refresh, and mapping save.
- SS images are scanned from `data/S.S IMAGE/` into `data/mappings.db`.
- Training page uses dropdown-based image selection instead of next/previous browsing.
- Image previews are clickable for fullscreen viewing and have copy actions.
- Bulk image export is available for visible image sets and the training queue.
- Tally refresh now falls back to the last saved export instead of breaking the UI.
- Training and viewer dropdowns use AJAX search with Select2 and immediate typing focus.
- Timestamp display now shows concrete `DD/MM/YYYY HH:MM:SS` values.
- Admin pricing page includes customer account table with delete and pause/resume controls.
- Bulk admin actions are available to pause/resume all customer accounts.
- Account actions are logged in database audit records.

## Current Workflow

1. Open `http://localhost:5000/login` and sign in as admin or customer.
2. Customer mode can inspect stock designs and scanned images only.
3. Admin mode can refresh stock and confirm mappings.
4. Open `http://localhost:5000/train` to browse/search the image queue, copy a single image, or export the full queue.
5. If Tally is unavailable, the app keeps working with the last saved local stock export and shows an admin warning.
6. Open `http://localhost:5000/admin/pricing` for pricing mode, account creation, account status controls, and account deletion.

## Active Endpoints

- `GET /login` and `POST /login` - role selection and sign-in.
- `GET /logout` - clear the current session.
- `GET /cars` - car list for the viewer.
- `GET /designs?car=NAME` - designs for a selected car.
- `POST /export_images` - downloads a ZIP of selected image files.
- `GET /train_images` - current unmapped training queue.
- `GET /get_image/<id>` - image preview for training.
- `POST /confirm_mapping` - admin-only save mapping.
- `GET /get_unmapped_images` - queue support for the trainer.
- `GET /mapping_stats` - training progress numbers.
- `POST /refresh_stock` - admin-only stock refresh with fallback.
- `GET /admin/get_all_customers` - account management table data.
- `POST /admin/delete_user/<id>` - admin-only customer deletion.
- `POST /admin/toggle_user_status/<id>` - admin-only pause/resume per customer.
- `POST /admin/set_all_customer_status` - admin-only bulk pause/resume.

## Notes

- Tally can be offline without breaking the viewer or trainer.
- The app will keep using the last saved upload until the live refresh succeeds again.
- Startup migration ordering issue for `users.created_at` was fixed; app initializes cleanly.
