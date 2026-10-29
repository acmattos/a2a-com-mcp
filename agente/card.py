"""The Agent Card (A2A v1.0, ProtoJSON): what the agent is, where it listens and what it can do."""

from a2a.types.a2a_pb2 import AgentCapabilities, AgentCard, AgentInterface, AgentProvider, AgentSkill

PROTOCOL_VERSION = "1.0"
TEXT = "text/plain"


def build_agent_card(rpc_url: str) -> AgentCard:
    return AgentCard(
        name="Central de Salas",
        description="Reserva salas de reuniao da Hill Valley Tech.",
        provider=AgentProvider(organization="Hill Valley Tech", url="https://hillvalley.example"),
        version="1.0.0",
        supported_interfaces=[
            AgentInterface(url=rpc_url, protocol_binding="JSONRPC", protocol_version=PROTOCOL_VERSION)
        ],
        # No streaming, no push, no extended card: the handlers below answer only SendMessage and GetTask.
        capabilities=AgentCapabilities(streaming=False, push_notifications=False, extended_agent_card=False),
        default_input_modes=[TEXT],
        default_output_modes=[TEXT],
        skills=[
            AgentSkill(
                id="reservar-sala",
                name="Reservar sala",
                description="Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                tags=["salas", "agenda"],
                input_modes=[TEXT],
                output_modes=[TEXT],
                examples=[
                    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                    "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
                ],
            )
        ],
    )

