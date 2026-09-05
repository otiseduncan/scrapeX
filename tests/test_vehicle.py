from scrapex.alldata import vehicle_matches
from scrapex.models import VehicleSpec

def test_model_punctuation():
    v=VehicleSpec(year=2018,make="Ford",model="F-350")
    assert vehicle_matches("2018 Ford Truck F350 4WD",v)

def test_make_alias():
    v=VehicleSpec(year=2018,make="Chevrolet",model="Tahoe")
    assert vehicle_matches("2018 Chevy Truck Tahoe 4WD",v)

def test_wrong_model():
    v=VehicleSpec(year=2018,make="Ford",model="F-350")
    assert not vehicle_matches("2018 Ford F150 4WD",v)
