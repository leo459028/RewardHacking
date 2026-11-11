"""v1.6.1 Enterprise Access: Dashboard accounts, sessions, sign-in providers and the permission table.

Modules: ``passwords`` (scrypt, D12 / D13), ``users`` (accounts, lockout, last-admin protection), ``sessions``
(server-side sessions, D15), ``principal`` (``Principal`` / ``Identity`` / ``LOCAL_ADMIN``, D16), ``providers``
(``AuthProvider`` / ``LocalPasswordProvider``) and ``permissions`` (the single permission table, D19-D22).
Nothing here imports the HTTP or Dashboard layers.
"""
