import sys
import traci

TLS_ID = "A0"
MIN_GREEN = 10
MAX_GREEN = 60
HOLD = 1000


def queue(lanes):
    return sum(traci.lane.getLastStepHaltingNumber(lane) for lane in lanes)


def main():
    gui = len(sys.argv) > 1 and sys.argv[1] == "gui"
    cmd = [
        "sumo-gui" if gui else "sumo",
        "-c", "baseline.sumocfg",
        "--tripinfo-output", "tripinfo_adaptive.xml",
        "--summary-output", "summary_adaptive.xml",
    ]
    if gui:
        cmd += ["--start", "--quit-on-end", "--delay", "50"]
    traci.start(cmd)

    controlled = traci.trafficlight.getControlledLanes(TLS_ID)
    phases = traci.trafficlight.getAllProgramLogics(TLS_ID)[0].phases
    green = {}
    for i, phase in enumerate(phases):
        if "G" in phase.state or "g" in phase.state:
            green[i] = {controlled[j] for j, c in enumerate(phase.state) if c in "Gg"}
    green_phases = list(green)

    last_phase = -1
    phase_start = 0
    while traci.simulation.getMinExpectedNumber() > 0:
        traci.simulationStep()
        now = traci.simulation.getTime()
        phase = traci.trafficlight.getPhase(TLS_ID)
        if phase != last_phase:
            last_phase = phase
            phase_start = now
            if phase in green:
                traci.trafficlight.setPhaseDuration(TLS_ID, HOLD)
        if phase in green and now - phase_start >= MIN_GREEN:
            other = green_phases[(green_phases.index(phase) + 1) % len(green_phases)]
            current_q = queue(green[phase])
            other_q = queue(green[other])
            if other_q > current_q or (now - phase_start >= MAX_GREEN and other_q > 0):
                traci.trafficlight.setPhase(TLS_ID, phase + 1)
    traci.close()


if __name__ == "__main__":
    main()
