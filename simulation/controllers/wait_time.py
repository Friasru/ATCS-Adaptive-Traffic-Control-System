import traci

MIN_GREEN = 20
MAX_GREEN = 45
SWITCH_MARGIN = 0
HOLD = 1000


class WaitTimeController:
    """Switches green based on accumulated vehicle waiting time per direction.

    Same phase structure as QueueBasedController: each green phase is followed
    by its yellow phase, so switching means advancing to phase + 1.
    """

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

    def wait(self, lanes):
        return sum(traci.lane.getWaitingTime(lane) for lane in lanes)

    def should_switch(self, elapsed, current_wait, other_wait):
        if elapsed < MIN_GREEN:
            return False
        if elapsed >= MAX_GREEN and other_wait > 0:
            return True
        # strict inequality: ties keep the current green (no needless switching)
        return other_wait > current_wait + SWITCH_MARGIN

    def step(self, now):
        phase = traci.trafficlight.getPhase(self.tls_id)
        if phase != self.last_phase:
            self.last_phase = phase
            self.phase_start = now
            if phase in self.green:
                traci.trafficlight.setPhaseDuration(self.tls_id, HOLD)
        if phase in self.green:
            other = self.green_phases[(self.green_phases.index(phase) + 1) % len(self.green_phases)]
            if self.should_switch(now - self.phase_start, self.wait(self.green[phase]), self.wait(self.green[other])):
                traci.trafficlight.setPhase(self.tls_id, phase + 1)
