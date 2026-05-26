# This file is intentionally empty.
# SMTP connection settings are now stored in the ``peripheral_config`` DB table
# (via ``/admin/peripherals/smtp``) and the SMTP password lives in the
# ``dataspoke-smtp-secret`` Kubernetes Secret.  NotificationService reads them
# at send time via peripheral_service.get_peripheral_config + smtp_secret.get_smtp_password.
