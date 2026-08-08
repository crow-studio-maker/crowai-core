# Deployment

Use a dedicated unprivileged OS account, HTTPS termination, a strong secret from a secret manager, private writable runtime directories, a read-only model/package mount where practical, and regular backups of `instance/workspace.db` plus uploads. Run with Waitress or another supported WSGI server; never expose Flask's development server or debugger to production.

Example:

```bash
export CROWAI_ENV=production
export CROWAI_SECRET_KEY='replace-with-a-long-secret-manager-value'
export CROWAI_INSTANCE_DIR=/srv/crowai/instance
export CROWAI_UPLOAD_DIR=/srv/crowai/uploads
export CROWAI_USERS_DIR=/srv/crowai/users
export CROWAI_MODELS_DIR=/srv/crowai/models
python serve.py
```

The in-process rate limiter is suitable for a local/single-process deployment. Multi-process or horizontally scaled deployments should replace it with a shared limiter and consider a shared session backend.

The model root is read-only input in production: CrowAI does not require write permission and does not create a missing production model directory. A production `DATABASE_PATH` must remain under `CROWAI_INSTANCE_DIR`; nested CrowAI-owned database directories are hardened to `0700` on POSIX.
