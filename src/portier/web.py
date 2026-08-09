"""Мини-панель реестра компаний: FastAPI + Jinja2 в том же процессе.

ВНИМАНИЕ (ограничение MVP): панель без авторизации, рассчитана на LAN.
"""

import logging
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from .models import Agent, Company

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "web_templates"
DEFAULT_SEED_FILE = "companies.yaml"


def create_app(session_factory) -> FastAPI:
    """Собрать приложение панели поверх фабрики сессий БД."""
    app = FastAPI(title="Portier AI — реестр компаний")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return RedirectResponse(url="/companies")

    @app.get("/companies", response_class=HTMLResponse)
    async def list_companies(request: Request):
        async with session_factory() as session:
            result = await session.execute(select(Company).order_by(Company.name))
            companies = result.scalars().all()
        return templates.TemplateResponse(
            request, "list.html", {"companies": companies}
        )

    @app.get("/companies/new", response_class=HTMLResponse)
    async def new_company_form(request: Request):
        return templates.TemplateResponse(
            request, "form.html", {"company": None, "action": "/companies/new"}
        )

    @app.post("/companies/new")
    async def create_company(
        name: str = Form(...),
        inn: str = Form(""),
        kpp: str = Form(""),
        legal_address: str = Form(""),
        details: str = Form(""),
        email: str = Form(""),
        subject_template: str = Form(""),
    ):
        async with session_factory() as session:
            session.add(Company(
                name=name, inn=inn, kpp=kpp, legal_address=legal_address,
                details=details, email=email, subject_template=subject_template,
            ))
            await session.commit()
        return RedirectResponse(url="/companies", status_code=303)

    @app.get("/companies/{company_id}/edit", response_class=HTMLResponse)
    async def edit_company_form(request: Request, company_id: int):
        async with session_factory() as session:
            company = await session.get(Company, company_id)
        if company is None:
            return RedirectResponse(url="/companies", status_code=303)
        return templates.TemplateResponse(
            request, "form.html",
            {"company": company, "action": f"/companies/{company_id}/edit"},
        )

    @app.post("/companies/{company_id}/edit")
    async def update_company(
        company_id: int,
        name: str = Form(...),
        inn: str = Form(""),
        kpp: str = Form(""),
        legal_address: str = Form(""),
        details: str = Form(""),
        email: str = Form(""),
        subject_template: str = Form(""),
    ):
        async with session_factory() as session:
            company = await session.get(Company, company_id)
            if company is not None:
                company.name = name
                company.inn = inn
                company.kpp = kpp
                company.legal_address = legal_address
                company.details = details
                company.email = email
                company.subject_template = subject_template
                await session.commit()
        return RedirectResponse(url="/companies", status_code=303)

    @app.post("/companies/{company_id}/delete")
    async def delete_company(company_id: int):
        async with session_factory() as session:
            company = await session.get(Company, company_id)
            if company is not None:
                await session.delete(company)
                await session.commit()
        return RedirectResponse(url="/companies", status_code=303)

    # --- Справочник агентов (тикет 13) ---

    @app.get("/agents", response_class=HTMLResponse)
    async def list_agents(request: Request):
        async with session_factory() as session:
            result = await session.execute(select(Agent).order_by(Agent.name))
            agents = result.scalars().all()
        return templates.TemplateResponse(
            request, "agents_list.html", {"agents": agents}
        )

    @app.get("/agents/new", response_class=HTMLResponse)
    async def new_agent_form(request: Request):
        return templates.TemplateResponse(
            request, "agents_form.html", {"agent": None, "action": "/agents/new"}
        )

    @app.post("/agents/new")
    async def create_agent(
        name: str = Form(...),
        aliases: str = Form(""),
        invoice_on_booking: str = Form(""),
        payer_name: str = Form(""),
        invoice_email: str = Form(""),
        price_note: str = Form(""),
        note: str = Form(""),
    ):
        async with session_factory() as session:
            session.add(Agent(
                name=name, aliases=aliases,
                invoice_on_booking=bool(invoice_on_booking),
                payer_name=payer_name, invoice_email=invoice_email,
                price_note=price_note, note=note,
            ))
            await session.commit()
        return RedirectResponse(url="/agents", status_code=303)

    @app.get("/agents/{agent_id}/edit", response_class=HTMLResponse)
    async def edit_agent_form(request: Request, agent_id: int):
        async with session_factory() as session:
            agent = await session.get(Agent, agent_id)
        if agent is None:
            return RedirectResponse(url="/agents", status_code=303)
        return templates.TemplateResponse(
            request, "agents_form.html",
            {"agent": agent, "action": f"/agents/{agent_id}/edit"},
        )

    @app.post("/agents/{agent_id}/edit")
    async def update_agent(
        agent_id: int,
        name: str = Form(...),
        aliases: str = Form(""),
        invoice_on_booking: str = Form(""),
        payer_name: str = Form(""),
        invoice_email: str = Form(""),
        price_note: str = Form(""),
        note: str = Form(""),
    ):
        async with session_factory() as session:
            agent = await session.get(Agent, agent_id)
            if agent is not None:
                agent.name = name
                agent.aliases = aliases
                agent.invoice_on_booking = bool(invoice_on_booking)
                agent.payer_name = payer_name
                agent.invoice_email = invoice_email
                agent.price_note = price_note
                agent.note = note
                await session.commit()
        return RedirectResponse(url="/agents", status_code=303)

    @app.post("/agents/{agent_id}/delete")
    async def delete_agent(agent_id: int):
        async with session_factory() as session:
            agent = await session.get(Agent, agent_id)
            if agent is not None:
                await session.delete(agent)
                await session.commit()
        return RedirectResponse(url="/agents", status_code=303)

    return app


async def seed_companies(session_factory, path: str = DEFAULT_SEED_FILE) -> int:
    """Сид реестра из companies.yaml (только если таблица пуста). Возвращает число добавленных."""
    if not Path(path).exists():
        return 0
    import yaml

    with open(path, encoding="utf-8") as fh:
        items = yaml.safe_load(fh) or []

    async with session_factory() as session:
        count = await session.execute(select(func.count(Company.id)))
        if count.scalar_one() > 0:
            logger.info("Реестр компаний не пуст, сид из %s пропущен", path)
            return 0
        for item in items:
            session.add(Company(
                name=item.get("name", ""),
                inn=item.get("inn", ""),
                kpp=item.get("kpp", ""),
                legal_address=item.get("legal_address", ""),
                details=item.get("details", ""),
                email=item.get("email", ""),
                subject_template=item.get("subject_template", ""),
            ))
        await session.commit()
    logger.info("Сид компаний из %s: добавлено %d", path, len(items))
    return len(items)
