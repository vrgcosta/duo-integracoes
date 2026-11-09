"""Detecção de RAM/VRAM e escolha do perfil (cpu ou gpu).

Tudo aqui é "melhor esforço": se não der para medir, o valor vira None e o
servidor segue com o perfil conservador (cpu). Detectar nunca pode impedir a
inicialização.
"""

import ctypes
import platform
import subprocess
from dataclasses import dataclass

# Padrões por perfil. O contexto menor e a espera maior no cpu refletem que o
# primeiro token demora mais quando o modelo roda no processador.
#
# Os modelos vêm da avaliação de 2026-10-23 (`avaliar_modelos.py`, tabela "Modelos testados" no
# README): os dois qwen3.5 passaram em todas as baterias (ferramentas nativas e por cercas, pt-BR,
# botão de pensamento). O qwen3:4b anterior saiu porque a etiqueta passou a apontar para o
# Qwen3-4B-Thinking-2507, que raciocina sempre: com o pensamento desligado, o raciocínio (em
# inglês) vaza no texto da resposta.
PERFIS: dict[str, dict] = {
    "cpu": {"modelo_padrao": "qwen3.5:4b", "contexto_tokens": 16384, "espera_primeiro_token": 600},
    "gpu": {"modelo_padrao": "qwen3.5:9b", "contexto_tokens": 32768, "espera_primeiro_token": 300},
}

# A partir daqui o perfil é gpu, e o 9B fica no limite: ele pede de 7,2 a 7,6 GB (avaliação de
# 2026-10-23), e uma placa de 8 GiB tem 8,6 GB, dos quais a área de trabalho e o navegador já usam
# parte. Uma fração do modelo pode ir para o processador, e a resposta fica mais lenta, mas não falha;
# o `--verificar` avisa quando isso acontece. Ficou assim por decisão (qualidade acima de folga):
# abaixo de 8 GB, nem o limite há, e o perfil é cpu.
VRAM_MINIMA_GPU_GB = 8
# No Apple Silicon a memória é unificada: não há VRAM separada para medir.
RAM_MINIMA_APPLE_GB = 16


@dataclass
class Hardware:
    ram_gb: float | None
    vram_gb: float | None
    gpu: str | None


def _ram_windows() -> float:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    estado = MEMORYSTATUSEX()
    estado.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(estado)):
        raise OSError("GlobalMemoryStatusEx falhou")
    return estado.ullTotalPhys / 1024**3


def _ram_linux() -> float:
    with open("/proc/meminfo", encoding="utf-8") as f:
        for linha in f:
            if linha.startswith("MemTotal:"):
                return int(linha.split()[1]) / 1024**2  # vem em kB
    raise OSError("MemTotal ausente em /proc/meminfo")


def _ram_macos() -> float:
    saida = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5, check=True)
    return int(saida.stdout.strip()) / 1024**3


def _ram_gb(sistema: str) -> float | None:
    leitores = {"Windows": _ram_windows, "Linux": _ram_linux, "Darwin": _ram_macos}
    try:
        return round(leitores[sistema](), 1)
    except Exception:
        return None


def _gpu_nvidia() -> tuple[str | None, float | None]:
    """Maior placa NVIDIA visível pelo nvidia-smi, como (nome, VRAM em GB)."""
    try:
        saida = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        melhor: tuple[str, float] | None = None
        for linha in saida.stdout.splitlines():
            nome, _, mib = linha.rpartition(",")
            vram = int(mib.strip()) / 1024  # nounits devolve MiB
            if melhor is None or vram > melhor[1]:
                melhor = (nome.strip(), vram)
        return (melhor[0], round(melhor[1], 1)) if melhor else (None, None)
    except Exception:
        return None, None


def detectar() -> Hardware:
    gpu, vram = _gpu_nvidia()
    return Hardware(ram_gb=_ram_gb(platform.system()), vram_gb=vram, gpu=gpu)


def escolher_perfil(hw: Hardware, sistema: str | None = None, maquina: str | None = None) -> str:
    # Resolvidos na chamada (e não como valor padrão): padrão de parâmetro é
    # avaliado uma vez na importação, o que impediria simular outro sistema.
    sistema = sistema or platform.system()
    maquina = maquina or platform.machine()
    if hw.vram_gb is not None and hw.vram_gb >= VRAM_MINIMA_GPU_GB:
        return "gpu"
    if sistema == "Darwin" and maquina == "arm64" and hw.ram_gb is not None and hw.ram_gb >= RAM_MINIMA_APPLE_GB:
        return "gpu"
    return "cpu"
