"""Offline installation smoke check: no .env, working database or remote requests."""

import argparse
import importlib
import json
import pkgutil
from importlib.metadata import version
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory


def selfcheck():
    report = {"mode": "OFFLINE_TEMPORARY_DATA", "status": "ERROR", "checks": {}, "external_requests": 0}
    try:
        from PIL import Image

        import app
        import playerokapi
        from app.database.store import Store
        from app.rendering.cards import profile_card
        from app.rendering.charts import revenue_chart

        checks = report["checks"]
        report["package_path"] = str(Path(app.__file__).resolve().parent)
        report["python_packages"] = {
            name: version(name) for name in ["aiogram", "httpx", "Pillow", "matplotlib", "python-dotenv"]
        }
        imported = []
        for module in pkgutil.walk_packages(app.__path__, app.__name__ + "."):
            importlib.import_module(module.name)
            imported.append(module.name)
        checks["application_modules_imported"] = len(imported)
        package = Path(app.__file__).parent
        migrations = sorted((package / "database" / "migrations").glob("[0-9]*.sql"))
        expected = [1] + [int(path.name.split("_", 1)[0]) for path in migrations]
        if not migrations or expected != list(range(1, max(expected) + 1)):
            raise ValueError("MIGRATION_FILES_INCOMPLETE")
        checks["migration_files"] = len(migrations)
        checks["certificate_present"] = (Path(playerokapi.__file__).parent / "cacert.pem").is_file()
        if not checks["certificate_present"]:
            raise ValueError("PACKAGED_CERTIFICATE_MISSING")
        with TemporaryDirectory(prefix="pcc-install-check-") as directory:
            database, backup = Path(directory) / "check.sqlite3", Path(directory) / "backup.sqlite3"
            store = Store(database)
            try:
                applied = [
                    row["version"]
                    for row in store.rows("SELECT version FROM schema_migrations ORDER BY version")
                ]
                if applied != expected:
                    raise ValueError("MIGRATION_APPLICATION_MISMATCH")
                checks["schema_versions"] = applied
                if store.rows("PRAGMA quick_check") != [{"quick_check": "ok"}] or store.rows(
                    "PRAGMA foreign_key_check"
                ):
                    raise ValueError("TEMPORARY_DATABASE_INTEGRITY_ERROR")
                store.backup(backup)
            finally:
                store.close()
            restored = Store(backup)
            try:
                if [
                    row["version"]
                    for row in restored.rows("SELECT version FROM schema_migrations ORDER BY version")
                ] != expected:
                    raise ValueError("BACKUP_SCHEMA_MISMATCH")
                checks["sqlite_backup_reopened"] = True
            finally:
                restored.close()
        for name, data in {
            "profile": profile_card({"username": "INSTALL CHECK", "rating": None, "reviews_count": None}),
            "chart": revenue_chart({"daily": {}, "categories": {}}),
        }.items():
            with Image.open(BytesIO(data)) as image:
                image.verify()
            checks[name + "_png"] = True
        report["status"] = "OK"
    except Exception as error:  # noqa: BLE001 -- diagnostic boundary returns type/code, never credentials or raw errors
        report["error_type"] = type(error).__name__
        if isinstance(error, ValueError) and str(error) in {
            "MIGRATION_FILES_INCOMPLETE",
            "PACKAGED_CERTIFICATE_MISSING",
            "MIGRATION_APPLICATION_MISMATCH",
            "TEMPORARY_DATABASE_INTEGRITY_ERROR",
            "BACKUP_SCHEMA_MISMATCH",
        }:
            report["error_code"] = str(error)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional local JSON report path")
    args = parser.parse_args()
    report = selfcheck()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)
    return 0 if report["status"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
