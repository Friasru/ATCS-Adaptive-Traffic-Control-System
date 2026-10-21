from app import db


class Scenario(db.Model):
    __tablename__ = "scenarios"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False)
    description = db.Column(db.String(255))
    ew_vehs_per_hour = db.Column(db.Integer, nullable=False)
    ns_vehs_per_hour = db.Column(db.Integer, nullable=False)
    runs = db.relationship("Run", backref="scenario", lazy=True)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "ew_vehs_per_hour": self.ew_vehs_per_hour,
            "ns_vehs_per_hour": self.ns_vehs_per_hour,
        }
