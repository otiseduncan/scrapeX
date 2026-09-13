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

def test_powertrain_variant_is_the_same_vehicle_family_for_adas_navigation():
    v=VehicleSpec(year=2023,make="Honda",model="Accord")
    assert vehicle_matches("2023 Honda Accord Sedan L4-1.5L Turbo",v)
    assert vehicle_matches("2023 Honda Accord Sedan Hybrid L4-2.0L Hybrid",v)
    assert not vehicle_matches("2024 Honda Accord Sedan Hybrid L4-2.0L Hybrid",v)
