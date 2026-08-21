import typer
from rich.console import Console
from rich.panel import Panel

from app.engine.orchestrator import IntelligenceEngine

app = typer.Typer()
console = Console()


@app.command()
def analyze(
    symbol: str = typer.Argument(
        ...,
        help="Bursa stock symbol, e.g. 5323 or 7081"
    )
):
    """Analyze a stock and generate an intelligence report."""

    engine = IntelligenceEngine()

    console.print(
        f"\n[cyan]Analyzing {symbol.upper()}...[/cyan]\n"
    )

    try:
        result = engine.analyze(symbol)

        console.print(
            Panel(
                result["report"],
                title=f"{symbol.upper()} Intelligence Report"
            )
        )

        console.print(
            f"\nReport saved: "
            f"[green]{result['report_path']}[/green]\n"
        )

    except Exception as e:
        console.print(
            f"\n[bold red]Analysis failed:[/bold red] {e}\n"
        )
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
