# Hybrid_-L3_Order-Book-for-High-Frequency-Trading
Notes AKA transformations:
1. Python Dictionary (self.orders = {})
The Python way: O(1) hash map lookup by order_id. The Verilog translation: Parallel Arrays (BRAMs) + Linear Scan FSM Verilog doesn't have native hash maps. Instead, I created a block of memory exactly 64 orders deep (ORDER_DEPTH = 64). I split the "Order" object into parallel arrays order_valid[], order_id_mem[], order_price[], order_qty[], and order_side[].

2. Python defaultdict (self.levels[price] += qty)
The Python way: Automatically creates a key for the price if it doesn't exist, and adds the quantity. The Verilog translation: Level Store Arrays + FSM Search Similar to the orders dict, I created a level_price[] and level_total_qty[] memory block.

When a new order arrives, the hardware FSM (in the S_ADD_LEVEL_SCAN state) physically iterates through the level_price array.
If it finds the price: it moves to S_ADD_LEVEL_UPDATE and adds w_qty.
If it hits the end without finding it: it moves to S_ADD_LEVEL_CREATE and writes a brand new entry at the level_count index.

3. Python Variables (self.bbo_bid_price, etc.)
The Python way: Standard variables. The Verilog translation: Dedicated Flip-Flop Registers This is where the FPGA crushes software. The variables best_bid_price, best_bid_qty, best_ask_price, and best_ask_qty are implemented as Flip-Flops instead of BRAM. Because they are flip-flops, the FPGA can physically wire the incoming cmd_price directly into a hardware comparator (>=) against the flip-flop output. This is why the "Surgical BBO Update" works — the hardware compares the new price, adds the new qty, and updates the BBO cache in exactly 1 clock cycle (10 nanoseconds) without looping through anything.

4. Python Heaps (heapq.heappush(self.bid_heap))
The Python way: Maintains an auto-sorting binary tree in memory. The Verilog translation: Lazy Linear Scan (S_REBUILD_SCAN) Implementing a dynamic sorted binary tree in Verilog is incredibly expensive (takes huge amounts of logic gates and routing overhead to constantly shift data around). So I deleted the heap concept entirely. Instead, because we cleverly designed the architecture to only need a rebuild when the BBO is completely wiped out, the hardware just drops into S_REBUILD_SCAN. It loops through the parallel 64-element level_price array and keeps a running track of the maximum/minimum price it sees (like a standard max() function). At 100 MHz, looping through 64 items theoretically takes 640 nanoseconds. 


### 1. When a new order improves the Best Price (e.g., higher Bid)
Instead of putting the order in memory and then scanning memory to figure out who is best, the FPGA has a physical wire running from the incoming `cmd_price` straight into a hardware comparator (`>=`) against a flip-flop called `best_bid_price`.
* **Hardware Action:** The comparator instantly says "True, this is better." The FSM flips the `best_bid_price` register to the new price in exactly **1 clock cycle (10ns)**. 
* *Zero scanning required.*

### 2. When a new order joins the exact same Best Price
* **Hardware Action:** The FSM takes the flip-flop `best_bid_qty`, routes it into a dedicated hardware adder (`+ cmd_qty`), and saves it back to `best_bid_qty` in **1 clock cycle (10ns)**.
* *Zero scanning required.*

### 3. When someone cancels an order deep in the book (Not the BBO)
Say the Best Bid is \$100. Someone cancels their order down at \$98.
* **Hardware Action:** We find their order and delete it from the BRAM. Then the hardware checks: `Is $98 == $100? No.` The FSM instantly finishes and does nothing to the BBO.
* *Zero BBO rebuild scanning required.*

### 4. When someone *partially* cancels the Best Price
Say the Best Bid is \$100 with 500 total shares. Someone cancels their 100-share order at \$100.
* **Hardware Action:** The hardware checks the `level_total_qty` remaining. Since 400 shares are still left, it physically routes `500 - 100` into a subtractor and updates the `best_bid_qty` register to 400 in **1 clock cycle (10ns)**.
* *Zero scanning required.*

---

### The ONLY time we use the Rebuild Scan (The 5%)
Say the Best Bid is \$100 with 100 shares. Someone cancels that exact 100-share order.
* **Hardware Action:** The level is now mathematically dead ($0 shares). The FPGA's O(1) cache doesn't know what the *next* best price is. 
* **Only in this specific moment** does the FSM drop into the linear scan loop (`S_REBUILD_SCAN`) to sift through all remaining price levels and find the new highest bid.

