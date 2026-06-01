#!/usr/bin/env python3
"""
Monitor system resource usage (CPU, RAM, disk I/O, etc.) over time.

This script helps measure current computer usage to determine hardware upgrade needs.
It collects metrics at regular intervals and saves them to a CSV file for analysis.

Usage:
    # Monitor for 1 hour, sampling every 10 seconds
    python execution/scripts/monitor_system_usage.py --duration 3600 --interval 10

    # Monitor continuously until stopped (Ctrl+C)
    python execution/scripts/monitor_system_usage.py

    # Monitor and show real-time stats
    python execution/scripts/monitor_system_usage.py --live

Output:
    CSV file: /tmp/system_usage_log.csv
    Summary report: /tmp/system_usage_summary.txt
"""

import argparse
import csv
import platform
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Output files
CSV_OUTPUT = Path("/tmp/system_usage_log.csv")
SUMMARY_OUTPUT = Path("/tmp/system_usage_summary.txt")


def get_cpu_usage() -> dict[str, float]:
    """Get CPU usage statistics."""
    try:
        # Use top command for CPU stats (works on macOS)
        result = subprocess.run(
            ["top", "-l", "1", "-n", "0"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            lines = result.stdout.split("\n")
            for line in lines:
                if "CPU usage" in line:
                    # Parse: "CPU usage: 45.23% user, 12.34% sys, 42.43% idle"
                    parts = line.split("CPU usage:")[1].strip()
                    user = float(parts.split("%")[0])
                    sys_part = parts.split("sys, ")[1].split("%")[0]
                    sys_usage = float(sys_part)
                    idle = float(parts.split("idle")[0].split()[-1].replace("%", ""))
                    return {
                        "cpu_user": user,
                        "cpu_sys": sys_usage,
                        "cpu_idle": idle,
                        "cpu_total": 100.0 - idle,
                    }
    except Exception as e:
        print(f"Warning: Could not get CPU usage: {e}", file=sys.stderr)

    # Fallback: use ps for overall CPU
    try:
        result = subprocess.run(
            ["ps", "-A", "-o", "%cpu"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")[1:]  # Skip header
            total_cpu = sum(float(line.strip()) for line in lines if line.strip())
            return {
                "cpu_user": total_cpu * 0.7,  # Estimate
                "cpu_sys": total_cpu * 0.3,  # Estimate
                "cpu_idle": 100.0 - total_cpu,
                "cpu_total": total_cpu,
            }
    except Exception:
        pass

    return {"cpu_user": 0.0, "cpu_sys": 0.0, "cpu_idle": 100.0, "cpu_total": 0.0}


def get_memory_usage() -> dict[str, float]:
    """Get memory (RAM) usage statistics."""
    try:
        # Use vm_stat on macOS
        result = subprocess.run(
            ["vm_stat"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            stats = {}
            for line in result.stdout.split("\n"):
                if ":" in line:
                    key, value = line.split(":", 1)
                    key = key.strip().replace(" ", "_").lower()
                    # Remove trailing period and convert to int
                    value = value.strip().rstrip(".").replace(",", "")
                    if value.isdigit():
                        stats[key] = int(value)

            # Calculate memory usage
            # Page size is typically 4096 bytes on macOS
            page_size = 4096

            # Get total physical memory
            total_mem_result = subprocess.run(
                ["sysctl", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            total_mem = 0
            if total_mem_result.returncode == 0:
                total_mem = int(total_mem_result.stdout.split(":")[1].strip())

            # Calculate used memory
            free_pages = stats.get("pages_free", 0) + stats.get("pages_inactive", 0)
            free_mem = free_pages * page_size
            used_mem = total_mem - free_mem

            return {
                "mem_total_gb": total_mem / (1024**3),
                "mem_used_gb": used_mem / (1024**3),
                "mem_free_gb": free_mem / (1024**3),
                "mem_used_percent": (
                    (used_mem / total_mem * 100) if total_mem > 0 else 0
                ),
            }
    except Exception as e:
        print(f"Warning: Could not get memory usage: {e}", file=sys.stderr)

    # Fallback: use top
    try:
        result = subprocess.run(
            ["top", "-l", "1", "-n", "0"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                if "PhysMem" in line:
                    # Parse: "PhysMem: 8192M used (2048M wired, 1024M compressor), 4096M free."
                    parts = line.split("PhysMem:")[1].strip()
                    used_part = parts.split(" used")[0]
                    used_mb = float(used_part.replace("M", "").replace("G", ""))
                    if "G" in used_part:
                        used_mb *= 1024
                    free_part = parts.split(" free")[0].split()[-1]
                    free_mb = float(free_part.replace("M", "").replace("G", ""))
                    if "G" in free_part:
                        free_mb *= 1024
                    total_mb = used_mb + free_mb
                    return {
                        "mem_total_gb": total_mb / 1024,
                        "mem_used_gb": used_mb / 1024,
                        "mem_free_gb": free_mb / 1024,
                        "mem_used_percent": (
                            (used_mb / total_mb * 100) if total_mb > 0 else 0
                        ),
                    }
    except Exception:
        pass

    return {
        "mem_total_gb": 0.0,
        "mem_used_gb": 0.0,
        "mem_free_gb": 0.0,
        "mem_used_percent": 0.0,
    }


def get_disk_usage() -> dict[str, float]:
    """Get disk usage statistics."""
    try:
        # Get disk usage for main volume
        result = subprocess.run(
            ["df", "-h", "/"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            if len(lines) >= 2:
                parts = lines[1].split()
                if len(parts) >= 5:
                    # Format: Filesystem Size Used Avail Capacity Mounted
                    size_str = parts[1]
                    used_str = parts[2]
                    avail_str = parts[3]

                    def parse_size(s: str) -> float:
                        """Parse size string like '500G', '1T', '228GI' to GB."""
                        s = s.upper().strip()
                        # Handle binary prefixes (Gi, Ti, Mi) - strip trailing 'I'
                        if s.endswith("I"):
                            s = s[:-1]
                        if s.endswith("T"):
                            return float(s[:-1]) * 1024
                        elif s.endswith("G"):
                            return float(s[:-1])
                        elif s.endswith("M"):
                            return float(s[:-1]) / 1024
                        elif s.endswith("K"):
                            return float(s[:-1]) / (1024**2)
                        else:
                            # Try to parse as number (assume bytes, convert to GB)
                            try:
                                return float(s) / (1024**3)
                            except ValueError:
                                return 0.0

                    total_gb = parse_size(size_str)
                    used_gb = parse_size(used_str)
                    free_gb = parse_size(avail_str)

                    return {
                        "disk_total_gb": total_gb,
                        "disk_used_gb": used_gb,
                        "disk_free_gb": free_gb,
                        "disk_used_percent": (
                            (used_gb / total_gb * 100) if total_gb > 0 else 0
                        ),
                    }
    except Exception as e:
        print(f"Warning: Could not get disk usage: {e}", file=sys.stderr)

    return {
        "disk_total_gb": 0.0,
        "disk_used_gb": 0.0,
        "disk_free_gb": 0.0,
        "disk_used_percent": 0.0,
    }


def get_disk_io() -> dict[str, float]:
    """Get disk I/O statistics."""
    try:
        # Use iostat on macOS (requires installing sysstat or using system_profiler)
        # Alternative: use top to get disk activity
        result = subprocess.run(
            ["top", "-l", "1", "-n", "0"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                if "Disks:" in line:
                    # Parse: "Disks: 71024334/1112G read, 17520600/374G written."
                    parts = line.split("Disks:")[1].strip()
                    read_part = parts.split(" read")[0].strip()
                    written_part = parts.split(" written")[0].split(",")[-1].strip()

                    # Extract size from format "operations/size" (e.g., "71024334/1112G")
                    def parse_disk_size(s: str) -> float:
                        """Parse disk size from format like '71024334/1112G' to MB."""
                        if "/" in s:
                            size_str = s.split("/")[1].upper().strip()
                            # Handle binary prefixes (Gi, Ti, Mi) - strip trailing 'I'
                            if size_str.endswith("I"):
                                size_str = size_str[:-1]
                            if size_str.endswith("T"):
                                return float(size_str[:-1]) * 1024 * 1024
                            elif size_str.endswith("G"):
                                return float(size_str[:-1]) * 1024
                            elif size_str.endswith("M"):
                                return float(size_str[:-1])
                            elif size_str.endswith("K"):
                                return float(size_str[:-1]) / 1024
                            else:
                                return float(size_str) / (1024**2)
                        return 0.0

                    read_mb = parse_disk_size(read_part)
                    write_mb = parse_disk_size(written_part)

                    return {
                        "disk_read_mb": read_mb,
                        "disk_write_mb": write_mb,
                    }
    except Exception as e:
        print(f"Warning: Could not get disk I/O: {e}", file=sys.stderr)

    return {"disk_read_mb": 0.0, "disk_write_mb": 0.0}


def get_network_io() -> dict[str, float]:
    """Get network I/O statistics."""
    try:
        # Use netstat or ifconfig
        result = subprocess.run(
            ["netstat", "-ib"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            total_bytes_in = 0
            total_bytes_out = 0
            for line in lines[1:]:  # Skip header
                parts = line.split()
                if len(parts) >= 10:
                    try:
                        bytes_in = int(parts[6])
                        bytes_out = int(parts[9])
                        total_bytes_in += bytes_in
                        total_bytes_out += bytes_out
                    except (ValueError, IndexError):
                        continue

            return {
                "net_bytes_in_mb": total_bytes_in / (1024**2),
                "net_bytes_out_mb": total_bytes_out / (1024**2),
            }
    except Exception as e:
        print(f"Warning: Could not get network I/O: {e}", file=sys.stderr)

    return {"net_bytes_in_mb": 0.0, "net_bytes_out_mb": 0.0}


def get_top_processes(count: int = 5) -> list[dict[str, any]]:
    """Get top processes by CPU and memory usage."""
    processes = []
    try:
        result = subprocess.run(
            ["ps", "-A", "-o", "pid,pcpu,pmem,comm", "-r"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")[1:]  # Skip header
            for line in lines[:count]:
                parts = line.split()
                if len(parts) >= 4:
                    try:
                        processes.append(
                            {
                                "pid": int(parts[0]),
                                "cpu_percent": float(parts[1]),
                                "mem_percent": float(parts[2]),
                                "name": " ".join(parts[3:]),
                            }
                        )
                    except (ValueError, IndexError):
                        continue
    except Exception as e:
        print(f"Warning: Could not get top processes: {e}", file=sys.stderr)

    return processes


def get_system_info() -> dict[str, str]:
    """Get system information."""
    info = {
        "platform": platform.system(),
        "platform_version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
    }

    # Get macOS-specific info
    if platform.system() == "Darwin":
        try:
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                info["cpu_model"] = result.stdout.strip()

            result = subprocess.run(
                ["sysctl", "-n", "hw.physicalcpu"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                info["cpu_cores_physical"] = result.stdout.strip()

            result = subprocess.run(
                ["sysctl", "-n", "hw.logicalcpu"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                info["cpu_cores_logical"] = result.stdout.strip()
        except Exception:
            pass

    return info


def collect_metrics() -> dict[str, any]:
    """Collect all system metrics."""
    timestamp = datetime.now()
    metrics = {
        "timestamp": timestamp.isoformat(),
        "unix_timestamp": timestamp.timestamp(),
    }

    # System info (only collect once, but include in each record for reference)
    metrics.update(get_cpu_usage())
    metrics.update(get_memory_usage())
    metrics.update(get_disk_usage())
    metrics.update(get_disk_io())
    metrics.update(get_network_io())

    return metrics


def write_csv_header(csv_file: Path, metrics: dict[str, any]):
    """Write CSV header."""
    with open(csv_file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=sorted(metrics.keys()))
        writer.writeheader()


def append_csv_row(csv_file: Path, metrics: dict[str, any]):
    """Append a row to CSV file."""
    file_exists = csv_file.exists()
    with open(csv_file, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=sorted(metrics.keys()))
        if not file_exists:
            writer.writeheader()
        writer.writerow(metrics)


def generate_summary(csv_file: Path, summary_file: Path):
    """Generate summary statistics from CSV data."""
    if not csv_file.exists():
        return

    import pandas as pd

    try:
        df = pd.read_csv(csv_file)
        df["timestamp"] = pd.to_datetime(df["timestamp"])

        summary_lines = []
        summary_lines.append("=" * 80)
        summary_lines.append("SYSTEM USAGE SUMMARY REPORT")
        summary_lines.append("=" * 80)
        summary_lines.append(
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        summary_lines.append(
            f"Data period: {df['timestamp'].min()} to {df['timestamp'].max()}"
        )
        summary_lines.append(f"Total samples: {len(df)}")
        summary_lines.append("")

        # CPU Statistics
        summary_lines.append("CPU USAGE:")
        summary_lines.append("-" * 80)
        summary_lines.append(f"  Average CPU Usage: {df['cpu_total'].mean():.2f}%")
        summary_lines.append(f"  Peak CPU Usage: {df['cpu_total'].max():.2f}%")
        summary_lines.append(f"  Median CPU Usage: {df['cpu_total'].median():.2f}%")
        summary_lines.append(
            f"  Time above 80%: {(df['cpu_total'] > 80).sum() / len(df) * 100:.1f}%"
        )
        summary_lines.append(
            f"  Time above 90%: {(df['cpu_total'] > 90).sum() / len(df) * 100:.1f}%"
        )
        summary_lines.append("")

        # Memory Statistics
        summary_lines.append("MEMORY (RAM) USAGE:")
        summary_lines.append("-" * 80)
        summary_lines.append(f"  Total RAM: {df['mem_total_gb'].iloc[0]:.2f} GB")
        summary_lines.append(
            f"  Average Used: {df['mem_used_gb'].mean():.2f} GB ({df['mem_used_percent'].mean():.2f}%)"
        )
        summary_lines.append(
            f"  Peak Used: {df['mem_used_gb'].max():.2f} GB ({df['mem_used_percent'].max():.2f}%)"
        )
        summary_lines.append(f"  Average Free: {df['mem_free_gb'].mean():.2f} GB")
        summary_lines.append(f"  Minimum Free: {df['mem_free_gb'].min():.2f} GB")
        summary_lines.append(
            f"  Time above 80%: {(df['mem_used_percent'] > 80).sum() / len(df) * 100:.1f}%"
        )
        summary_lines.append(
            f"  Time above 90%: {(df['mem_used_percent'] > 90).sum() / len(df) * 100:.1f}%"
        )
        summary_lines.append("")

        # Disk Statistics
        summary_lines.append("DISK USAGE:")
        summary_lines.append("-" * 80)
        summary_lines.append(f"  Total Disk: {df['disk_total_gb'].iloc[0]:.2f} GB")
        summary_lines.append(
            f"  Average Used: {df['disk_used_gb'].mean():.2f} GB ({df['disk_used_percent'].mean():.2f}%)"
        )
        summary_lines.append(f"  Average Free: {df['disk_free_gb'].mean():.2f} GB")
        summary_lines.append(f"  Minimum Free: {df['disk_free_gb'].min():.2f} GB")
        summary_lines.append("")

        # Recommendations
        summary_lines.append("UPGRADE RECOMMENDATIONS:")
        summary_lines.append("-" * 80)

        cpu_avg = df["cpu_total"].mean()
        cpu_peak = df["cpu_total"].max()
        mem_avg = df["mem_used_percent"].mean()
        mem_peak = df["mem_used_percent"].max()
        mem_total = df["mem_total_gb"].iloc[0]

        recommendations = []

        if cpu_peak > 90:
            recommendations.append(
                "⚠️  HIGH PRIORITY: CPU frequently maxed out (>90%). Consider upgrading CPU."
            )
        elif cpu_avg > 70:
            recommendations.append(
                "⚠️  CPU usage is high on average (>70%). Consider faster CPU or more cores."
            )
        elif cpu_peak > 80:
            recommendations.append(
                "ℹ️  CPU occasionally reaches high usage (>80%). Monitor during heavy workloads."
            )

        if mem_peak > 95:
            recommendations.append(
                "⚠️  HIGH PRIORITY: RAM frequently maxed out (>95%). Urgently need more RAM."
            )
        elif mem_avg > 80:
            recommendations.append(
                "⚠️  RAM usage is consistently high (>80%). Consider upgrading RAM."
            )
        elif mem_peak > 85:
            recommendations.append(
                "ℹ️  RAM occasionally reaches high usage (>85%). Consider more RAM for headroom."
            )

        if mem_total < 8:
            recommendations.append(
                "ℹ️  Current RAM ({:.1f} GB) is below modern standards. Consider 16GB+ for better performance.".format(
                    mem_total
                )
            )
        elif mem_total < 16:
            recommendations.append(
                "ℹ️  Consider upgrading to 16GB+ RAM for smoother multitasking and future-proofing."
            )

        if not recommendations:
            recommendations.append(
                "✅ System resources appear adequate for current usage patterns."
            )

        for rec in recommendations:
            summary_lines.append(f"  {rec}")

        summary_lines.append("")
        summary_lines.append("=" * 80)

        summary_file.write_text("\n".join(summary_lines))
        print(f"\n✅ Summary report saved to: {summary_file}")

    except ImportError:
        print(
            "Warning: pandas not available. Install with: pip install pandas",
            file=sys.stderr,
        )
    except Exception as e:
        print(f"Error generating summary: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Monitor system resource usage over time",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=None,
        help="Duration to monitor in seconds (default: monitor until Ctrl+C)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=10,
        help="Sampling interval in seconds (default: 10)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Show real-time statistics",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=CSV_OUTPUT,
        help=f"Output CSV file path (default: {CSV_OUTPUT})",
    )

    args = parser.parse_args()

    # Print system info
    print("System Information:")
    print("-" * 80)
    sys_info = get_system_info()
    for key, value in sys_info.items():
        print(f"  {key}: {value}")
    print()

    # Initialize CSV file
    print("Starting system monitoring...")
    print(f"  Output file: {args.output}")
    print(f"  Sampling interval: {args.interval} seconds")
    if args.duration:
        print(f"  Duration: {args.duration} seconds")
    else:
        print("  Duration: Until stopped (Ctrl+C)")
    print()

    start_time = time.time()
    sample_count = 0

    try:
        while True:
            # Collect metrics
            metrics = collect_metrics()
            append_csv_row(args.output, metrics)
            sample_count += 1

            # Display live stats if requested
            if args.live:
                print(
                    f"[{sample_count}] {metrics['timestamp']} | "
                    f"CPU: {metrics['cpu_total']:.1f}% | "
                    f"RAM: {metrics['mem_used_percent']:.1f}% ({metrics['mem_used_gb']:.1f}GB/{metrics['mem_total_gb']:.1f}GB) | "
                    f"Disk: {metrics['disk_used_percent']:.1f}%"
                )

            # Check duration
            if args.duration:
                elapsed = time.time() - start_time
                if elapsed >= args.duration:
                    break

            # Wait for next sample
            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n\nMonitoring stopped by user.")

    # Generate summary
    print(f"\nCollected {sample_count} samples.")
    print("Generating summary report...")
    generate_summary(args.output, SUMMARY_OUTPUT)
    print(f"\n✅ Data saved to: {args.output}")
    print(f"✅ Summary saved to: {SUMMARY_OUTPUT}")


if __name__ == "__main__":
    main()
