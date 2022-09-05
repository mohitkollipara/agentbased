import random
import time
from queue import Queue

import numpy as np

from .contact_graph import get_contacts_from_unit_contact_dicts, contact_tracing
from .statemachine import TestingState
from .utils import random_susceptibles, func_timer
from .calibration import effective_contact_rate_new


class TruthClassStatus:
    """
    Keeps tract of all case statistics
    """

    def __init__(self):
        # super().__init__()

        self.HQuarantinedP = []
        self.AFreeP = []
        self.AQuarantinedP = []
        self.SIsolatedP = []
        self.SHospitalizedP = []
        self.SIcuP = []
        self.RRecoveredP = []
        self.RDiedP = []
        self.SFreeP = []
        self.testing_result_flag = 0


class VirusModel(TruthClassStatus):
    """
    All virus related routines
    """

    def __init__(self):

        super().__init__()
        self.NewInfection = 0
        self.Esymptomstime = self.pm.Virus_IncubationPeriod
        self.CureTime = self.pm.Virus_ExpectedCureDays
        self.ProbSeverity = self.pm.Virus_ProbSeverity
        self.AgeDR = self.pm.Virus_PerDayDeathRate
        self.FullCapRatio = self.pm.Virus_FullCapRatio
        self.TestingOn = False
        self.TracingOn = False
        self.pool_size = 5
        self.dorfman_set_count = 0

        # Day wise placeholder
        self.Symptom_placeholder = [[] for _ in range(90)]
        self.Recovered_Placeholder = [[] for _ in range(90)]
        self.Deaths_Placeholder = [[] for _ in range(90)]
        self.TestingQueue = Queue(maxsize=0)
        self.PositivePlaceholder = [[] for _ in range(self.SIM_DAYS)]
        self.testing_model = {1: self.random_testing, 2: self.symptom_testing, 3: self.hostel_testing,
                              4: self.contacttracing_testing , 5:self.risk_testing}
        self.not_tested_list = []
        self.positiveon_result_day = []
        self.dorfman_pool_list = []

        # Vaccination Parameters
        self.Num_Vaccinated_Once = 0
        self.num_of_people_in_queue = 0
        self.Num_Vaccinated_Twice = 0

        self.is_validation_sim = False

        self.free_people_list = []

    def apply_dr_multiplier(self, person, deathrate: float):
        """Apply Death Rate multipler to a person' death rate based on his comobrbidity

        Args:
            person (object): Person ojbect
            deathrate (float): current deathrate

        Returns:
            float: new deathrate
        """

        # TODO: Fix Hardcoding
        multiplier = 1
        if person.is_Isolation() and len(self.SIsolatedP) > self.sectors['Healthcare'].Capacity['Care_Center']:
            multiplier = self.FullCapRatio[0]

        elif person.is_Hospitalized() and len(self.SHospitalizedP) > self.sectors['Healthcare'].Capacity['Health_Center']:
            multiplier = self.FullCapRatio[1]

        elif person.is_ICU() and len(self.SIcuP) > self.sectors['Healthcare'].Capacity['Hospital']:
            multiplier = self.FullCapRatio[2]

        return deathrate * multiplier

    def __infect_person__(self, person):
        """
        State change of a person from healthy to infected

        Args:
            person (person): person ojbect

        Returns:
            int: 0 if person wasn't able to infect due to already infected or out of Region. 1 otherwise
        """
        if not person.is_Out_of_Campus():
            if person.is_Healthy():
                Symptom = int(
                    np.random.normal(self.Esymptomstime[person.AgeClass], self.Esymptomstime[person.AgeClass] / 3))
                # print("The value of Symptom is {} and the value of esymptomstime is {}".format(Symptom, self.Esymptomstime[person.AgeClass]), person.AgeClass,self.Esymptomstime)
                if Symptom < 0:
                    Symptom = 0

                self.Symptom_placeholder[Symptom].append(person)
                person.infected()
                # self.RepoRateSum+=1
                return 1
            else:
                return 0
        else:
            return 0

    @staticmethod
    def __get_contacts__(person):
        """
        Function to get the contacts of a person on a particular day
        Query MySQL database -> get contacts and their edge weights
        """

        # start = time.process_time()
        # contacts, edge_weights = get_contacts_from_server(person.ID, time_datetime, self.db_conn, begin_time=begin_time)
        # end = time.process_time()
        # print ("Time elapsed to get contacts from server:", end - start)

        # start = time.process_time()
        # contacts, edge_weights = get_contacts_without_using_server(person)
        contacts, edge_weights = get_contacts_from_unit_contact_dicts(person)
        # end = time.process_time()
        # print ("Time elapsed to get_contacts_from_unit_contact_dicts:", end - start)

        return contacts, edge_weights

    def has_symptoms(self, person, cure: int):
        """Subroutine to change the state of person to symptomatic

        Args:
            person (object): person object who has shown symptons
            cure (int): days after which the person would be cured
        """
        if person.is_Out_of_Campus():
            person.quarantined()
            return

        prob_severity = person.get_prob_severity(self.ProbSeverity[person.AgeClass])
        deathrate = person.get_death_rate(self.AgeDR[person.AgeClass])
        deathrate = self.apply_dr_multiplier(person, deathrate)
        if person.Role == "desk_worker" or person.Role == "non_desk_worker":
            choice = person.isolate
        else:
            choice = random.choices([person.isolate, person.hospitalized, person.admit_icu], weights=prob_severity)[0]
        # if(choice==person.quarantined and person.State=='Asymptomatic' ):
        # 	self.
        choice()

        if person.is_quarantined():
            person.show_symptoms()

        if self.TestingOn:
            self.put_to_test(person, "Fresh")

        if cure < 0:
            cure = 0

        deathrate = self.apply_dr_multiplier(person, deathrate)

        sampledeaths = random.choices([True, False], [deathrate, 1 - deathrate], k=cure)
        # Died with probablity death rate, True if died on that day
        try:  # Look for index of True , if present died before cure
            deathday = sampledeaths.index(True)
            self.Deaths_Placeholder[deathday].append(person)  # Append Death Day
        except ValueError:
            self.Recovered_Placeholder[cure].append(person)  # If not found, all is false, append cureday

    def __daily_hospitals_check__(self):
        """Checks for all people who are supposed to be cured or died today i.e reach terminal state of statemachine
        """
        today_cured = self.Recovered_Placeholder.pop(0)
        for person in today_cured:
            person.recover()

        today_died = self.Deaths_Placeholder.pop(0)
        for person in today_died:
            person.die()

    def __daily_symptoms_check__(self):
        """Checks for people whose symptoms have shown today. These people either will go to ICU, Hospital or remain at home
        """
        today_symptoms = self.Symptom_placeholder.pop(0)

        # print('In region {}, {} people are added to testing list'.format(self.Name, len(today_symptoms)))
        curearray = np.random.normal(self.CureTime, self.CureTime / 3, size=len(today_symptoms))
        for i, person in enumerate(today_symptoms):
            self.has_symptoms(person, int(curearray[i]))

    def __daily_transmissions__(self, c_calibrated=1):
        temp_AFreeP = self.AFreeP.copy() + self.SFreeP.copy()
        for person in temp_AFreeP:
            if not person.inCampus:
                continue
            # print('Person id = {}'.format(person.ID))
            # contacts_idx, edge_weights = self.__get_contacts__(person, begin_time=begin_time)
            contacts_idx, edge_weights = self.__get_contacts__(person)
            # print(contacts_idx)

            # TODO: For each contact get P(transmission) from calibration.py as a function of interperson distance
            #  and time of contact P_TR = 0.015 # TODO: Dummy value for now start = time.process_time()
            ct = 0
            for idx in contacts_idx:
                P_TR = c_calibrated * edge_weights[ct]
                contact = self.__get_person_obj__(idx=idx)
                P_TR *= self.__get_vaccination_factor__(contact)
                infect_bool = random.choices([True, False], weights=[P_TR, 1 - P_TR])[0]
                if infect_bool:
                    if self.__infect_person__(contact):
                        self.NewInfection += 1
                    self.infect_dict[person.ID] = self.infect_dict.get(person.ID, 0) + 1
                else:
                    self.infect_dict[person.ID] = self.infect_dict.get(person.ID, 0)

                ct += 1
        # print(self.infect_dict)

        # end = time.process_time()
        # print("Time elapsed to infect:", end - start)
        # time_datetime = datetime.datetime.fromtimestamp(time.mktime(self.curr_timestamp))
        # if time_datetime-begin_time == datetime.timedelta(days=1):
        # 	db_cursor.execute("DELETE FROM activity WHERE time BETWEEN '{}' AND '{}'".format(begin_time,begin_time+datetime.timedelta(days=1)))

    @func_timer
    def daily_transmissions(self):
        self.__daily_hospitals_check__()
        self.__daily_symptoms_check__()
        self.__daily_transmissions__(c_calibrated=self.pm.Virus_c)
        self.daily_visitor_transmissions(c_calibrated=self.pm.Virus_c)
        self.__building_random_transmissions__(c_calibrated=self.pm.Virus_c)
        self.common_area_transmissions(c_calibrated=self.pm.Virus_c)

        self.Symptom_placeholder.append([])
        self.Deaths_Placeholder.append([])
        self.Recovered_Placeholder.append([])

    def __get_vaccination_factor__(self, person):
        """
        Helper function to return the factor by which TR is multiplied to add the effect of vaccination
        """

        if person.Vaccination_State == "Not_Vaccinated":
            return 1
        elif person.Vaccination_State == "Partially_Vaccinated":
            return 1 - self.pm.Efficiency_Dose_1
        elif person.Vaccination_State == "Fully_Vaccinated":
            return 1 - self.pm.Efficiency_Dose_2
        else:
            print("INVALID VACCINATION STATE")
            raise AssertionError

    def __random_sampling_vaccination__(self, acceptable_vaccination_states):
        """
        Random Sampling based vaccination paradigm
        1. Vaccine Capacity = X/day
        2. Every day sample X people from the Unvaccinated, Free People and adminster the vaccine
            a. For the sake of simplicity do this with the counter approach for now, can refine later
        3. After everyone is vaccinated once, (maintain a flag?) sample vaccinated people again

        """

        ctr = 200
        vaccinated_today = []
        num_vaccinated_today = 0
        while num_vaccinated_today < self.pm.Num_Daily_Vaccinations:

            # Sample people from entire population, and vaccinate them if they are Not_Vaccinated and Free (
            # sick/isolated people wouldn't be vaccinated for now)
            people_to_vaccinate = random.choices(self.all_people, k=2 * self.pm.Num_Daily_Vaccinations)
            for person in people_to_vaccinate:
                if (person.Vaccination_State not in acceptable_vaccination_states) or person.Status != "Free":
                    continue
                if person.Vaccination_State == "Not_Vaccinated":
                    person.Gets_First_Dose()
                    self.Num_Vaccinated_Once += 1
                elif person.Vaccination_State == "Partially_Vaccinated":
                    person.Gets_Second_Dose()
                    self.Num_Vaccinated_Twice += 1
                else:
                    print("VACCINATION FAILED, invalid acceptable vaccination state")
                    raise AssertionError
                num_vaccinated_today += 1
                vaccinated_today.append(person.ID)
                if num_vaccinated_today == self.pm.Num_Daily_Vaccinations:
                    break

            # Prevent this loop from going on forever
            ctr -= 1
            if ctr < 1:
                print("VACCINATION FAILED, not enough people")
                print(acceptable_vaccination_states)
                print(vaccinated_today)
                print(self.Num_Vaccinated_Once)
                print(self.pm.Second_Dose_Threshold * len(self.all_people))
                raise AssertionError
                return

        if self.pm.Verbose:
            print("Number of people vaccinated on day {} = {}".format(self.TODAY, num_vaccinated_today))
            print("People vaccinated today = {}".format(vaccinated_today))

    @func_timer
    def daily_vaccination(self):
        """
        Module to handle the vaccination process

        """
        if self.pm.Vaccination_Paradigm is None:
            pass

        elif self.pm.Vaccination_Paradigm == "Random_Sampling":
            acceptable_vaccination_states = ["Not_Vaccinated"]
            if self.Num_Vaccinated_Once > self.pm.Second_Dose_Threshold * len(self.all_people):
                acceptable_vaccination_states.append("Partially_Vaccinated")

            self.__random_sampling_vaccination__(acceptable_vaccination_states)

    @func_timer
    def daily_testing(self):
        time_in_sec = time.mktime(self.curr_timestamp)
        day = time.strftime("%A", time.localtime(time_in_sec)).casefold()
        if self.testing_result_flag == 1:  # this flag is for whether to call Testing_result() or not
            if self.pm.is_dorfman_pooling:
                self.dorfman_testing_result()
            else:
                self.Testing_result()
        if self.pm.Exact_Test_Details.get("Date of Testing", False) and len(
                self.pm.Exact_Test_Details["Date of Testing"]):
            if self.pm.Exact_Test_Details["Date of Testing"][-1] == time.strftime("%m/%d/%Y", self.curr_timestamp):
                self.is_validation_sim = True
                self.testing_model[self.pm.testing_strategy]()
        elif day in self.pm.Testing_days:
            self.testing_model[self.pm.testing_strategy]()


    def symptom_testing(self):
        ##############
        # Testing_Strategy 2 CODE #
        ##############
        self.all_testing_attributes()
        self.all_testing_methods()

    def random_testing(self):
        ##############
        # Testing_Strategy 1 CODE #
        ##############
        self.testing_result_flag = 1
        self.num_of_people_in_queue = 0
        self.all_testing_methods()

    def contacttracing_testing(self):
        ##############
        # Testing_Strategy 3 CODE #
        ##############
        self.all_testing_attributes()
        edge_threshold = 0.2
        if len(self.positiveon_result_day) != 0:
            for i in self.positiveon_result_day[-1]:
                contacts, edge_weights = contact_tracing(i)
                for j in range(len(contacts)):
                    temp_person_obj = self.__get_person_obj__(contacts[j])
                    if edge_weights[j] > edge_threshold and (temp_person_obj.state == 'Not_tested' or temp_person_obj.re_test == 1) and temp_person_obj.state != "Awaiting_Testing":
                        self.TestingQueue.put(temp_person_obj)
                        temp_person_obj.awaiting_test()
                        self.update_re_test_attribute()  # function executes, if everyone is tested .i.e.. Num_not_tested = 0
                        temp_person_obj.re_test = 0
                        self.num_of_people_in_queue = self.num_of_people_in_queue + 1
        self.all_testing_methods()

    # [n clusters] i clusters contain symptomatic and asymptomatic then we have to test all the members of the
    # clusters rest n-i clusters are healthy or recovered only, will check  entire cluster with random probability does
    # this cluster have positive or negative

    def dorfman_testing_result(self):
        specificity = self.pm.test_specificity
        sensitivity = self.pm.test_sensitivity
        k = 0
        l = []
        testCap, students_only = self.check_validation()
        while k <= testCap and self.TestingQueue.qsize():  # [[h,H,h],[h,H,h],[h,H,h],[h,H,h],[h,H,h]]
            l.append(self.TestingQueue.get_nowait())
            if len(l) == self.pool_size:
                self.dorfman_pool_list.append(l.copy())
                l = []
            k = k + 1
        if len(l) > 0:
            self.dorfman_pool_list.append(l.copy())
            l = []
        self.testing_result_flag = 0
        check = [False for _ in range(len(self.dorfman_pool_list))]
        s = -1
        for i in self.dorfman_pool_list:
            s += 1
            flag = False
            for j in i:
                if j.State == j.states[2] or j.State == j.states[1]:
                    flag = True
                    break
            if flag:
                if random.random() < sensitivity:
                    check[s] = True
                continue
            if random.random() > specificity:
                check[s] = True
        s = 0
        for i in self.dorfman_pool_list:
            if check[s]:
                for j in i:
                    if j.State == j.states[0]:
                        if random.random() < specificity:
                            j.tested_negative()
                            j.attr_risk = 0
                            j.free()
                        else:
                            j.tested_positive()
                            j.attr_risk = 0
                            self.PositivePlaceholder[self.TODAY - 1].append(j)
                            j.quarantined()
                    elif j.State == j.states[3]:
                        j.tested_negative()
                        j.attr_risk = 0
                        j.free()
                    else:
                        if j.State == j.states[4]:
                            pass
                        else:
                            if random.random() < sensitivity:
                                j.tested_positive()
                                j.attr_risk = 0
                                self.PositivePlaceholder[self.TODAY - 1].append(j)
                                if (j.Status == "Free") and (j.State == 'Symptomatic'):  # symptomatic free person
                                    j.isolate()
                                if j.State == j.states[1]:  # asymptomatic and both free & quarantined person
                                    j.quarantined()
                            else:
                                j.tested_negative()
                                j.attr_risk = 0
                                if (j.State == 'Symptomatic') and (
                                        j.Status == 'Isolation'):  # symptomatic person in isolation
                                    j.free()
                                elif (j.State == j.states[1]) and (
                                        j.Status == 'Quarantined'):  # asymptomatic person in quarantine
                                    j.free()
            else:
                for j in i:
                    j.tested_negative()
                    j.attr_risk = 0
                    j.free()

            s += 1
        self.dorfman_pool_list.clear()
        self.positiveon_result_day.append(self.PositivePlaceholder[self.TODAY - 1])

    def Testing_result(self):
        specificity = self.pm.test_specificity
        sensitivity = self.pm.test_sensitivity
        k = 0
        self.testing_result_flag = 0  # this flag is for whether to call Testing_result()
        test_cap, students_only = self.check_validation()
        while k <= test_cap and self.TestingQueue.qsize():
            temp = self.TestingQueue.get_nowait()
            if students_only and temp.Role != "student":
                continue
            if temp.State == temp.states[0]:  # healthy person
                if random.random() < specificity:
                    temp.tested_negative()
                    temp.attr_risk = 0
                    temp.free()
                else:
                    temp.tested_positive()
                    temp.attr_risk = 0
                    self.PositivePlaceholder[self.TODAY - 1].append(temp)
                    temp.quarantined()

            elif temp.State == temp.states[3]:  # recovered
                temp.tested_negative()
                temp.attr_risk = 0

            else:
                if temp.State == temp.states[4]:  # dead person
                    pass
                else:  # asymptomatic and symptomatic
                    if random.random() < sensitivity:  #
                        temp.tested_positive()
                        temp.attr_risk = 0
                        self.PositivePlaceholder[self.TODAY - 1].append(temp)
                        if temp.Status == "Free" and temp.State == 'Symptomatic':  # symptomatic free person
                            temp.isolate()
                        if temp.State == temp.states[1]:  # asymptomatic and both free & quarantined person
                            temp.quarantined()
                    else:
                        temp.tested_negative()
                        temp.attr_risk = 0
                        if ((temp.State == 'Symptomatic') and (
                                temp.Status == 'Isolation')):  # symptomatic person in isolation
                            temp.free()
                        elif ((temp.State == temp.states[1]) and (
                                temp.Status == 'Quarantined')):  # asymptomatic person in quarantine
                            temp.free()
            k += 1
        self.positiveon_result_day.append(self.PositivePlaceholder[self.TODAY - 1])

        if self.pm.Exact_Test_Details.get("Date of Testing", False) and len(
                self.pm.Exact_Test_Details["Date of Testing"]):
            if time.strftime("%m/%d/%Y", self.curr_timestamp) in self.TestingLog["Date of Testing"]:
                for key in self.pm.Exact_Test_Details:
                    self.pm.Exact_Test_Details[key].pop()

    def risk_testing(self):
        ##############
        # Testing_Strategy 4 CODE #
        ##############
        threshold =  84.0899878909384
        self.all_testing_attributes()
        if len(self.positiveon_result_day) != 0:
            for i in self.positiveon_result_day[-1]:
                for j in self.all_people:    # TODO: optimization
                    if (j.state == "Not_tested" or j.re_test == 1) and j.state != "Awaiting_Testing":
                        j.attr_risk += self.attribute_matrix[i.ID][j.ID]
                        if j.attr_risk > threshold:
                            j.awaiting_test()
                            self.update_re_test_attribute()  # function executes, only if everyone is tested .
                            j.re_test = 0
                            self.TestingQueue.put(j)
                            self.num_of_people_in_queue += 1
        self.all_testing_methods()

    def hostel_testing(self):
        ##############
        # Testing_Strategy 5 CODE #
        ##############
        self.all_testing_attributes()
        students = self.hostels[list(self.hostels.keys())[self.TODAY%len(self.hostels)]]
        test_cap, students_only = self.check_validation()
        new_list = []
        for i in students:
            if (self.ID_to_person[i].state == 'Not_Tested' or self.ID_to_person[i].re_test ==1) and self.ID_to_person[i].state != "Awaiting_Testing":
                new_list.append(self.ID_to_person[i])
        if len(new_list) > test_cap-self.num_of_people_in_queue:
            list_key = random.sample(new_list,test_cap-self.num_of_people_in_queue)
            self.num_of_people_in_queue =  test_cap
        else :
            list_key = new_list
            self.num_of_people_in_queue = self.num_of_people_in_queue + len(list_key)
        for id in list_key:
            id.awaiting_test()
            id.re_test = 0
            self.TestingQueue.put(id)
        self.update_re_test_attribute()
        self.all_testing_methods()

    def random_enqueuing_and_random_people_retest_without_posipeople(self):
        loop_ctr = 0
        test_cap, students_only = self.check_validation()
        while self.num_of_people_in_queue < test_cap :
            if loop_ctr >= 2 * len(self.all_people):
                break
            person = random.choice(self.all_people)
            if students_only and person.Role != "student":
                continue
            if person.state == "Not_tested":
                person.awaiting_test()
                person.re_test = 0
                self.TestingQueue.put(person)
                self.num_of_people_in_queue += 1
            if (self.pm.selecting_retest_random_people is True
                    and person.re_test == 1
                    and person.state != 'Tested_Positive'
                    and person.state != 'Awaiting_Testing'):  # random_people_retest , it is one of retesting method###
                person.awaiting_test()
                person.re_test = 0
                self.TestingQueue.put(person)
                self.num_of_people_in_queue += 1

            self.update_re_test_attribute()   # function executes, if everyone is tested .i.e.. Num_not_tested = 0
            loop_ctr += 1
        # while self.num_of_people_in_queue < test_cap:
        #     person = random.choice(self.all_people)
        #     if students_only and person.Role != "student":
        #         continue
        #     if (self.pm.selecting_retest_random_people is True
        #             and person.state != 'Tested_Positive'
        #             and person.state != 'Awaiting_Testing'):  # random_people_retest , it is one of retesting method
        #         person.awaiting_test()
        #         person.re_test = 0
        #         self.TestingQueue.put(person)
        #         self.num_of_people_in_queue += 1
        # self.update_re_test_attribute()  # function executes, if everyone is tested .i.e.. Num_not_tested = 0
        # print("no.of people in queue ", self.num_of_people_in_queue)

    def random_enqueuing_and_random_people_retest_with_positivepeople(self):
        loop_ctr = 0
        test_cap, students_only = self.check_validation()
        while self.num_of_people_in_queue < test_cap :
            if loop_ctr >= 2 * len(self.all_people):
                break
            person = random.choice(self.all_people)
            if students_only and person.Role != "student":
                continue
            if person.state == "Not_tested":
                person.awaiting_test()
                person.re_test = 0
                self.TestingQueue.put(person)
                self.num_of_people_in_queue += 1
            if (self.pm.selecting_retest_random_people is True
                    and person.re_test == 1
                    and person.state != 'Awaiting_Testing'):  # random_people_retest, it is one of retesting method
                person.awaiting_test()
                person.re_test = 0
                self.TestingQueue.put(person)
                self.num_of_people_in_queue += 1

            self.update_re_test_attribute()  # function executes, if everyone is tested .i.e.. Num_not_tested = 0
            loop_ctr += 1
        # while self.num_of_people_in_queue < test_cap:
        #     person = random.choice(self.all_people)
        #     if students_only and person.Role != "student":
        #         continue
        #     if (self.pm.selecting_retest_random_people is True
        #             and person.state != 'Awaiting_Testing'):  # random_people_retest , it is one of retesting method
        #         person.awaiting_test()
        #         person.re_test = 0
        #         self.TestingQueue.put(person)
        #         self.num_of_people_in_queue += 1
        # self.update_re_test_attribute()  # function executes, if everyone is tested .i.e.. Num_not_tested = 0
        # print("no.of people in queue ", self.num_of_people_in_queue)

    def all_testing_attributes(self):
        self.testing_result_flag = 1
        self.num_of_people_in_queue = 0
        self.symptomatic_people_into_queue()

    def all_testing_methods(self):
        if self.pm.retesting_positive_people_14_days_back and self.pm.selecting_random_people:
            self.fourteen_days_back_positive_tested()
            self.random_enqueuing_and_random_people_retest_without_posipeople()  # see line 418
        elif self.pm.retesting_positive_people_14_days_back:
            self.fourteen_days_back_positive_tested()
        elif self.pm.selecting_random_people:
            self.random_enqueuing_and_random_people_retest_with_positivepeople()  # see line 432

    def update_re_test_attribute(self):
        if TestingState.Num_not_tested == 0:
            for i in self.all_people:
                i.re_test = 1
                if (i.state == "Not_tested" or i.re_test == 1) and i.Role == "student":
                    TestingState.Num_not_tested += 1

    def symptomatic_people_into_queue(self):

        TestCap, students_only = self.check_validation()

        for i in self.SIsolatedP + self.SHospitalizedP:
            if not i.inCampus:
                continue
            if students_only and i.Role != "student":
                continue
            if i.state == "Not_tested" :
                self.TestingQueue.put(i)
                i.awaiting_test()
                self.update_re_test_attribute()  # function executes, if everyone is tested .
                i.re_test = 0
                self.num_of_people_in_queue += 1
            if self.pm.retesting_negative_people:  # one of the retesting method
                if i.state == "Tested_Negative" and i.re_test == 1:
                    self.TestingQueue.put(i)
                    i.awaiting_test()
                    self.update_re_test_attribute()  # function executes, if everyone is tested .
                    i.re_test = 0
                    self.num_of_people_in_queue += 1

    def fourteen_days_back_positive_tested(self):  # one of the restesting method
        if self.TODAY - 14 > 0:
            for i in self.positiveon_result_day[0]:
                self.TestingQueue.put(i)
                self.num_of_people_in_queue += 1
                i.awaiting_test()
                self.update_re_test_attribute()  # function executes, if everyone is tested i.e.. Num_not_tested = 0
                i.re_test = 0
            self.positiveon_result_day.pop(0)

    def daily_visitor_transmissions(self, c_calibrated=1):
        """
        Transmits virus from random walk visitors to people in Campus
        """
        for person_obj in self.visitors:
            for t in range(24):
                if person_obj.today_schedule.get(t, -1) != -1:
                    bldg_id = person_obj.today_schedule[t]
                    bldg_population = []
                    for unit_id in person_obj.Campus.Units_Placeholder[bldg_id]:
                        bldg_population.extend(
                            person_obj.Campus.Units_Placeholder[bldg_id][unit_id].visiting.get(t, []))
                    if person_obj.Campus.buildings.get(bldg_id, None) is None:
                        continue
                    bldg_area = person_obj.Campus.buildings[bldg_id].building_area_in_sqm2
                    bldg_density = len(bldg_population) / bldg_area
                    num_of_susceptibles = random_susceptibles(Density=bldg_density, Population=len(bldg_population),
                                                              ComplianceRate=self.pm.Initial_Compliance_Rate)
                    susceptibles = random.sample(bldg_population, k=num_of_susceptibles)
                    self.__hourly_random_transmissions__(susceptibles, c=c_calibrated)

    def __building_random_transmissions__(self, c_calibrated=1, calibrate=0):
        temp_AFreeP = self.AFreeP.copy() + self.SFreeP.copy()
        if calibrate == 1:
            temp_AFreeP = self.all_people.copy()
        for person_obj in temp_AFreeP:
            for t in range(24):
                if person_obj.today_schedule.get(t, -1) != -1:
                    bldg_id = person_obj.today_schedule[t].Building
                    if bldg_id is None or bldg_id in self.isolation_centre_ids + self.quarantine_centre_ids:
                        continue
                    floor = person_obj.today_schedule[t].height
                    floor_population = []
                    for unit_id in self.Units_Placeholder[bldg_id]:
                        if self.Units_Placeholder[bldg_id][unit_id].height == floor:
                            floor_population.extend(self.Units_Placeholder[bldg_id][unit_id].visiting.get(t, []))
                    if self.buildings.get(bldg_id, None) is None:
                        continue
                    bldg_area = person_obj.Campus.buildings[bldg_id].building_area_in_sqm2
                    bldg_density = len(floor_population) / bldg_area
                    num_of_susceptibles = random_susceptibles(Density=bldg_density, Population=len(floor_population),
                                                              ComplianceRate=self.pm.Initial_Compliance_Rate)
                    if calibrate == 1:
                        self.no_people_inf_by_person[person_obj.ID] += num_of_susceptibles*effective_contact_rate_new(gamma=1, avg_interperson_distance=self.pm.avg_interpdist_bldg_transmission, avg_exp_time=self.pm.avg_exptime_bldg_transmission, pm=self.pm, virus_c=c_calibrated)
                        continue
                    susceptibles = random.sample(floor_population, k=num_of_susceptibles)
                    self.__hourly_random_transmissions__(susceptibles, c=c_calibrated)

    def common_area_transmissions(self, c_calibrated=1):

        self.free_people_list = []
        for person in self.all_people:
            if person.Status == "Free":
                self.free_people_list.append(person)

        if self.pm.CampusName == "IIITH":

            if self.Lockdown:
                lockdown_status = "AfterLockdown"
            else:
                lockdown_status = "BeforeLockdown"

            for bldg_id in self.pm.common_areas_info:
                bldg_info = self.pm.common_areas_info[bldg_id][lockdown_status]
                # bldg_area = self.buildings[bldg_id].building_area_in_sqm2
                unit_area = list(self.Units_Placeholder[bldg_id].values())[0].area

                num_people_going_there  = bldg_info["AvgNumPeoplePerDay"]
                if num_people_going_there != 0:
                    num_batches = bldg_info["ActiveDuration"] / bldg_info["AvgTimeSpent"]  # instead of sending everyone at once
                    num_people_per_batch = int(num_people_going_there // num_batches)
                else:
                    continue

                if num_people_per_batch == 0:
                    num_people_per_batch = num_people_going_there
                while num_people_going_there > 0:
                    if num_people_going_there < num_people_per_batch:
                        num_people_per_batch = num_people_going_there

                    bldg_density = num_people_per_batch / unit_area
                    num_of_susceptibles = random_susceptibles(
                        Density=bldg_density, Population=num_people_per_batch,
                        ComplianceRate=self.pm.Initial_Compliance_Rate
                    )

                    susceptibles = random.sample(self.free_people_list, k=num_of_susceptibles)
                    actual_susceptibles = []
                    num_infected = 0
                    while len(susceptibles):
                        temp_person = susceptibles.pop()
                        if temp_person.State in ["Asymptomatic", "Symptomatic"]:
                            num_infected+=1
                        else:
                            actual_susceptibles.append(temp_person.ID)
                    for _ in range(num_infected):
                        self.__hourly_random_transmissions__(actual_susceptibles, c=c_calibrated, avg_dist=1, avg_time=bldg_info["AvgTimeSpent"])  #TODO: avg_dist is hard coded as 1m change it later

                    num_people_going_there-=num_people_per_batch

    def __hourly_random_transmissions__(self, contacts_idx, c=1, avg_dist=None, avg_time=None):
        ct = 0
        if avg_dist is None or avg_time is None:
            avg_dist = self.pm.avg_interpdist_bldg_transmission
            avg_time = self.pm.avg_exptime_bldg_transmission
        for idx in contacts_idx:
            P_TR = effective_contact_rate_new(gamma=1, avg_interperson_distance=avg_dist, avg_exp_time=avg_time, pm=self.pm, virus_c=c)
            contact = self.__get_person_obj__(idx=idx)
            P_TR *= self.__get_vaccination_factor__(contact)
            infect_bool = random.choices([True, False], weights=[P_TR, 1 - P_TR])[0]
            if infect_bool:
                # print("Infecting person {}".format(contact.ID))
                if self.__infect_person__(contact):  # checks whether the contacts is getting infected
                    self.NewInfection += 1
            ct += 1

    def check_validation(self):
        if self.pm.CampusName == "IIITH":
            if self.is_validation_sim and len(self.pm.Exact_Test_Details["Total"]):
                test_cap = self.pm.Exact_Test_Details["Total"][-1]
                students_only = False
            else:
                test_cap = self.pm.Testing_capacity
                students_only = False
        else:  # self.pm.CampusName == "IITJ"
            if self.is_validation_sim and len(self.pm.Exact_Test_Details["Number of Student's Tested"]):
                test_cap = self.pm.Exact_Test_Details["Number of Student's Tested"][-1]
                students_only = True
            else:
                test_cap = self.pm.Testing_capacity
                students_only = False

        return test_cap, students_only

