"""Inspect Django's installed migrations without querying a database.

Executed inside the application image or through ECS Exec by shared actions;
never installed as an application management command. Format 1 matches H2O's
existing manifests. Migration names, including third-party apps, are the contract.
"""
import hashlib
import json

import django
from django.db.migrations.loader import MigrationLoader

django.setup()
names = sorted('.'.join(key) for key in MigrationLoader(None, ignore_no_migrations=True).disk_migrations)
payload = ''.join(f'{name}\n' for name in names).encode()
print('DJANGO_MANIFEST=' + json.dumps({
    'format': 1,
    'hash': hashlib.sha256(payload).hexdigest()[:12],
    'count': len(names),
    'migrations': names,
}))
