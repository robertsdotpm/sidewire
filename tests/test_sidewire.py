from aionetiface import *

# Server used for tests.
MQTT_SERVER = ("ovh1.p2pd.net", 1883)

class TestSignaling(unittest.IsolatedAsyncioTestCase):
    async def test_something(self):
        #nic = await Interface()
        #print(nic)
        #return


        nic = await Interface("default")
        print(nic.supported())

        
        con = await Pipe(TCP, MQTT_SERVER, nic).connect()
        print(con.sock)

if __name__ == '__main__':
    main()


