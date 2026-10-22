import traci

MIN_GREEN = 20
MAX_GREEN = 60
SWITCH_MARGIN = 2
HOLD = 1000


class QueueBasedController:
    def __init__(self, tls_id):
        self.tls_id = tls_id
        controlled = traci.trafficlight.getControlledLanes(tls_id)
        phases = traci.trafficlight.getAllProgramLogics(tls_id)[0].phases
        self.green = {}
        for i, phase in enumerate(phases):
            if "G" in phase.state or "g" in phase.state:
                self.green[i] = {controlled[j] for j, c in enumerate(phase.state) if c in "Gg"}
        self.green_phases = list(self.green)
        self.last_phase = -1
        self.phase_start = 0

    def queue(self, lanes):
        return sum(traci.lane.getLastStepHaltingNumber(lane) for lane in lanes)

    def step(self, now):
        phase = traci.trafficlight.getPhase(self.tls_id)
        if phase != self.last_phase:
            self.last_phase = phase
            self.phase_start = now
            if phase in self.green:
                traci.trafficlight.setPhaseDuration(self.tls_id, HOLD)
        if phase in self.green and now - self.phase_start >= MIN_GREEN:
            other = self.green_phases[(self.green_phases.index(phase) + 1) % len(self.green_phases)]
            current_q = self.queue(self.green[phase])
            other_q = self.queue(self.green[other])
            if other_q >= current_q + SWITCH_MARGIN or (now - self.phase_start >= MAX_GREEN and other_q > 0):
                traci.trafficlight.setPhase(self.tls_id, phase + 1)
