"""One-time trusted local bootstrap; later administration occurs in WhatsApp."""

import argparse

from sqlalchemy import select

from church_roster_bot.core import models  # noqa: F401
from church_roster_bot.core.database import SessionLocal
from church_roster_bot.members.models import GeneralAdmin
from church_roster_bot.ministries.models import Church
from church_roster_bot.whatsapp.payloads import normalize_phone


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--church-id", type=int, required=True)
    args = parser.parse_args()
    phone = normalize_phone(input("WhatsApp E.164 do primeiro administrador: ").strip())
    with SessionLocal.begin() as db:
        if db.get(Church, args.church_id) is None:
            raise SystemExit("Igreja não encontrada")
        if db.scalar(select(GeneralAdmin.id).where(GeneralAdmin.church_id == args.church_id)):
            raise SystemExit("Já existe um administrador geral. Use o fluxo autorizado do bot.")
        db.add(GeneralAdmin(church_id=args.church_id, phone_e164=phone))
    print("Administrador inicial cadastrado.")


if __name__ == "__main__":
    main()
