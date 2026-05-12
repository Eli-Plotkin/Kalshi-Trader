


def run_agent_trader():
    '''
        Get markets, for each:
            1) Run sonnet to decide on a plan to evaluate if there is a profitable trade. 
            If there is one, write down criteria for a future LLM to evaluate this research data.
                - Use assumptions data
                - Use current positions and historical data
            if no viable plan - skip
            2) if viable plan exists, delegate subagent to execute research plan. Provide tools necessary.
            Execute research plan.
            3) Query new LLM (sonnet) with the data, market price, and criteria for evaluating
                - If not enough info, skip
                - Decide buy/sell/hold
                - Log Decision and logic

        At end of day:
            for each trade that closed:
                - Log what the trade was, what the logic was, and if/where an error was made
                - Rewrite the prompts for 1  and 3 to take into account errors in the planning or critera process
                
    '''
    
    return




if __name__ == "__main__":
    run_agent_trader()