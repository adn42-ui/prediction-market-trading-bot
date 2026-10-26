import unittest
from datetime import date
from recommender import executable_prices, recommend, Estimate

class Tests(unittest.TestCase):
    def test_orderbook_direct(self):
        self.assertEqual(executable_prices({"yesAsks":[{"price":.61}],"noAsks":[{"price":.41}],"yesBids":[{"price":.59}],"noBids":[{"price":.39}]}),(.61,.41,.59,.39))
    def test_complement_price(self):
        self.assertEqual(executable_prices({"asks":[[.62,100]],"bids":[[.58,100]]}),(.62,.42,.58,.38))
    def test_edge_after_cost(self):
        e=Estimate(.70,"high",date.today().isoformat(),"reason",[],[])
        r=recommend("x",e,.60,.42)
        self.assertEqual(r["side"],"YES")
        self.assertAlmostEqual(r["edge"],.10)
    def test_unmodeled_no_trade(self):
        self.assertEqual(recommend("x",None,.4,.6)["side"],"NO TRADE")

if __name__ == '__main__': unittest.main()
