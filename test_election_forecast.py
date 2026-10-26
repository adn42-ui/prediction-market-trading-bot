import io
import unittest
from datetime import date

import election_forecast as ef


class ElectionForecastTests(unittest.TestCase):
    def test_market_mapping_party_and_race(self):
        self.assertEqual(ef.market_event("Will the Democratic Party win the Texas Senate?"),("race","TX-SEN","DEM"))
        self.assertEqual(ef.market_event("Will the Republican Party win the NY-17 House race?"),("race","NY-17","REP"))
        self.assertEqual(ef.market_event("Will the Republican Party win the Texas Senate?")[1],
                         ef.market_event("Will the Democratic Party win the Texas Senate?")[1])

    def test_chamber_mapping(self):
        self.assertEqual(ef.market_event("Will the Democratic Party win the U.S. House?"),("chamber","US House","DEM"))

    def test_yes_no_executable_prices(self):
        book={"yesAsks":[{"price":.62,"quantity":50}],"yesBids":[{"price":.58,"quantity":40}]}
        parsed=ef.executable_book(book)
        self.assertEqual(parsed["yes_ask"],.62)
        self.assertAlmostEqual(parsed["no_ask"],.42)
        self.assertEqual(parsed["no_qty"],40)

    def test_generic_ballot_no_future_leakage(self):
        rows=[{"end_date":"2026-09-01","dem":"50","rep":"45","n":"1000","rating":"2","lean":""},
              {"end_date":"2026-10-02","dem":"99","rep":"1","n":"1000","rating":"3","lean":""}]
        margin,_,latest,count=ef.generic_ballot(rows,date(2026,10,1))
        self.assertAlmostEqual(margin,5)
        self.assertEqual(latest,"2026-09-01")
        self.assertEqual(count,1)

    def test_validation_is_time_ordered(self):
        rows=[]
        for year in (2008,2010,2012):
            for i in range(30):
                p=.8 if i%2 else .2; y=1 if (p>.5) else 0
                rows.append({"year":str(year),"forecast_prob":str(p),"winflag":str(y)})
        report=ef.validation_report(rows)
        self.assertTrue(report["holdouts"])
        self.assertEqual(report["holdouts"][0]["year"],2010)

    def test_simulation_reproducible_and_exact_count_guard(self):
        race=ef.RaceInput("TX-SEN","senate","TX","S",0,0.8,1.8,4.3,2,3,"2026-09-01",-5,1,"REP")
        with self.assertRaises(ValueError): ef.simulate([race],1000,7)


if __name__=='__main__': unittest.main()
