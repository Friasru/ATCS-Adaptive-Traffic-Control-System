from datetime import datetime, timezone

from app import db


class Run(db.Model):
    __tablename__ = "runs"

    id = db.Column(db.Integer, primary_key=True)
    scenario_id = db.Column(db.Integer, db.ForeignKey("scenarios.id"), nullable=False)
    controller = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="pending")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    vehicles_completed = db.Column(db.Integer)
    avg_wait_s = db.Column(db.Float)
    max_wait_s = db.Column(db.Float)
    avg_time_loss_s = db.Column(db.Float)
    avg_trip_duration_s = db.Column(db.Float)

    def to_dict(self):
        return {
            "id": self.id,
            "scenario_id": self.scenario_id,
            "controller": self.controller,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "vehicles_completed": self.vehicles_completed,
            "avg_wait_s": self.avg_wait_s,
            "max_wait_s": self.max_wait_s,
            "avg_time_loss_s": self.avg_time_loss_s,
            "avg_trip_duration_s": self.avg_trip_duration_s,
        }
