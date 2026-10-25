"""Human-readable report presentation; statistics and authorization stay in scheduling."""

from church_roster_bot.scheduling.reports import AuditPage, MonthlyReport


def format_monthly_report(report: MonthlyReport) -> str:
    lines = [
        f"RELATÓRIO — {report.ministry_name} — {report.month:%m/%Y}",
        f"Capacidade: {report.needed}; posições ocupadas: {report.occupied} "
        f"(inclui {report.pending_confirmations} reserva(s) aguardando confirmação).",
        "Frequência mensal: dias distintos de compromissos confirmados, "
        "presenças registradas ou pedidos de substituição ainda sob responsabilidade do original.",
        "Uma confirmação não comprova presença. Presenças são registradas à parte.",
    ]
    for row in report.volunteers:
        lines.append(
            f"#{row.volunteer_id} {row.name}: {row.distinct_days} dia(s); "
            f"{row.confirmed_tasks} função(ões) confirmada(s); "
            f"{row.completed_tasks} presença(s) registrada(s); {row.absences} ausência(s); "
            f"{row.pending_tasks} reserva(s); {row.replacement_tasks} aguardando troca."
        )
    lines.extend(
        [
            f"Equilíbrio entre {report.balance_population} voluntário(s) ativo(s) com permissão: "
            f"média {report.balance_mean:.2f} dia(s), diferença máxima {report.balance_spread}, "
            f"desvio padrão {report.balance_stddev:.2f}. Inclui quem tem carga zero.",
            f"Posições sem preenchimento: {sum(v.positions for v in report.vacancies)}.",
        ]
    )
    for vacancy in report.vacancies:
        lines.append(
            f"Vaga #{vacancy.slot_id} — {vacancy.service_date:%d/%m/%Y} — "
            f"{vacancy.role}: {vacancy.positions} posição(ões)."
        )
    labels = {"pending": "pendentes", "fulfilled": "concluídas", "cancelled": "canceladas"}
    lines.append(
        "Substituições: "
        + (
            ", ".join(
                f"{labels.get(status, status)}: {count}" for status, count in report.replacements
            )
            or "nenhuma"
        )
    )
    lines.append(
        f"Convites recusados: {report.declined_invitations}; "
        f"reservas recusadas: {report.declined_reservations}."
    )
    return "\n".join(lines)


def format_audit(page: AuditPage) -> str:
    lines = ["AUDITORIA — eventos mais recentes do escopo autorizado (horários UTC)"]
    for row in page.events:
        lines.append(
            f"#{row.id} {row.created_at:%Y-%m-%d %H:%M:%S} UTC — "
            f"{row.actor_type} #{row.actor_id}: {row.action}; "
            f"{row.entity_type} #{row.entity_id}. {row.details}"
        )
    if not page.events:
        lines.append("Nenhum evento registrado neste escopo.")
    if page.next_before_id:
        lines.append(f"Para eventos anteriores: AUDITORIA {page.next_before_id}.")
    return "\n".join(lines)
